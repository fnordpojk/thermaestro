"""An export replayed: the learners run over it day by day, in the time it was recorded.

Everything but the history, the forecasts' leads and what was decided is loaded first: the
settings, the descriptions, intents, the forecasts' and prices' intervals. The history then
goes in a day at a time, only what had been recorded by then (and the forecasts' leads
measured by then), and at the end of each day, an hour on, as the core does, the inputs
are checked and the learners run with the clock at that moment. So nothing learns from
what came after: replaying fewer days gives the same values for those days.

Written to the output directory:
- `audit.json`: the export's SHA-256, its header, the days replayed, and each point's
  coverage (its first and last sample, how many, the hours it covered);
- `learned.jsonl`: every learned value after each day: its state, value and what is used;
- `inputs.json`: how each input was judged at the end;
- `learned.json`: what was learned at the end, beside what the installation had learned
  when it was exported;
- `scorecard.json`, given labels: the hot-water draws found against them;
- `replay.db`: the replay's database, to look into.
"""

import asyncio
import gzip
import hashlib
import json
import logging
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from zoneinfo import ZoneInfo

from .. import clock
from ..cap.messages import Described
from ..cap.model import Envelope
from ..cap.vocabulary import QUANTITIES
from ..core.audit import AuditLog
from ..core.values import Key, Values
from ..core.weather import Forecast, Span
from ..learn import Learning
from ..store import Database, Location, Transaction
from ..store.settings import SETTINGS
from .export import FORMAT
from .labels import Label, score

log = logging.getLogger(__name__)

AFTER_S = 3600.0
"""Each day is learned from an hour after it ends, as the core reads a day."""


class ReplayError(Exception):
    pass


@dataclass
class Clock:
    t: float = 0.0

    def time(self) -> float:
        return self.t

    def monotonic(self) -> float:
        return self.t


class ReplayWeather:
    """The forecasts the learners ask for, from the exported intervals: the first provider
    that has the quantity."""

    def __init__(self, db: Database) -> None:
        self._db = db

    async def forecast(self, quantity: str, start: datetime, end: datetime) -> Forecast:
        t0, t1 = start.timestamp(), end.timestamp()

        def read(t: Transaction) -> list[tuple[str, float, float, float]]:
            return list(
                t.execute(
                    "SELECT instance, start, end, value FROM intervals"
                    " WHERE (series = ? OR series LIKE ?) AND end > ? AND start < ?"
                    " ORDER BY instance, start",
                    (quantity, f"%/{quantity}", t0, t1),
                )
            )

        rows = await self._db.run(read)
        if not rows:
            return Forecast(quantity, None)
        source = rows[0][0]
        spans = [
            Span(datetime.fromtimestamp(a, UTC), datetime.fromtimestamp(b, UTC), v)
            for s, a, b, v in rows
            if s == source
        ]
        return Forecast(quantity, source, spans=spans)


@dataclass
class Export:
    header: dict[str, Any]
    described: dict[str, Described] = field(default_factory=dict)
    records: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    sha256: str = ""


def read(path: Path) -> Export:
    """An export, all but the history read into memory; the history is read again as it
    is replayed."""
    digest = hashlib.sha256()
    with path.open("rb") as raw:
        for chunk in iter(lambda: raw.read(1 << 20), b""):
            digest.update(chunk)
    found: Export | None = None
    for item in _lines(path):
        kind = item["record"]
        if kind == "header":
            if item.get("format") != FORMAT:
                raise ReplayError(f"{path} isn't a Thermaestro export")
            found = Export(item, sha256=digest.hexdigest())
        elif found is None:
            raise ReplayError(f"{path} doesn't start with its header")
        elif kind == "described":
            found.described[item["instance"]] = Described.model_validate(item["body"])
        elif kind not in ("history", "history_15m"):
            found.records.setdefault(kind, []).append(item)
    if found is None:
        raise ReplayError(f"{path} is empty")
    return found


def _fresh(out: Path) -> Path:
    """The output directory, with no replay database left in it from before."""
    out.mkdir(parents=True, exist_ok=True)
    database = out / "replay.db"
    for stale in (database, out / "replay.db-wal", out / "replay.db-shm"):
        stale.unlink(missing_ok=True)
    return database


def _lines(path: Path) -> Iterator[dict[str, Any]]:
    with gzip.open(path, "rt", encoding="utf-8") as f:
        for line in f:
            if line.strip():
                yield json.loads(line)


async def replay(
    source: Path, out: Path, *, labels: list[Label] | None = None, days: int | None = None
) -> dict[str, Any]:
    """Replay `source` into `out`; the audit's summary."""
    found = read(source)
    database = await asyncio.to_thread(_fresh, out)
    zone = ZoneInfo(found.header.get("timezone", "UTC"))
    start, end = float(found.header["start"]), float(found.header["end"])
    first = datetime.fromtimestamp(start, zone).date() + timedelta(days=1)
    last = datetime.fromtimestamp(end - AFTER_S, zone).date() - timedelta(days=1)
    every = [first + timedelta(days=i) for i in range((last - first).days + 1)]
    if days is not None:
        every = every[:days]
    timer = Clock(start)
    with clock.simulated(timer):
        async with await Database.open(database) as db:
            await _load(db, found)
            await _stage(db, source)
            values = Values(db)
            host = SimpleNamespace(
                instances={
                    i: SimpleNamespace(described=d) for i, d in sorted(found.described.items())
                }
            )
            units = _units(found.described)
            learning = Learning(
                db, values, cast(Any, host), AuditLog(out / "audit"), cast(Any, ReplayWeather(db))
            )
            await learning.load()
            timeline = (out / "learned.jsonl").open("w", encoding="utf-8")
            with timeline:
                for day in every:
                    now = datetime.combine(day + timedelta(days=1), datetime.min.time(), zone)
                    timer.t = now.timestamp() + AFTER_S
                    await _feed(db, timer.t)
                    values.latest = await _latest(db, units)
                    await learning.check_inputs()
                    await learning.learn()
                    for x in learning.learnings.all():
                        timeline.write(
                            json.dumps(
                                {
                                    "day": day.isoformat(),
                                    "name": x.name,
                                    "scope": x.scope,
                                    "state": x.state,
                                    "value": x.value,
                                    "used": x.used,
                                    "why": x.why,
                                    "args": x.args,
                                }
                            )
                            + "\n"
                        )
            checks = [
                {
                    "instance": c.instance,
                    "point": c.point,
                    "verdict": c.verdict,
                    "why": c.why,
                    "args": dict(c.args),
                }
                for c in learning.checks.values()
            ]
            (out / "inputs.json").write_text(json.dumps(checks, indent=1), encoding="utf-8")
            exported = {r["key"]: r["body"] for r in found.records.get("learned", [])}
            ended = {x.key: x.model_dump(mode="json") for x in learning.learnings.all()}
            (out / "learned.json").write_text(
                json.dumps({"replayed": ended, "exported": exported}, indent=1), encoding="utf-8"
            )
            coverage = await _coverage(db)
            summary: dict[str, Any] = {
                "source": str(source),
                "sha256": found.sha256,
                "header": found.header,
                "days": [d.isoformat() for d in every],
                "coverage": coverage,
            }
            if labels is not None:
                card = await score(db, labels, zone)
                (out / "scorecard.json").write_text(json.dumps(card, indent=1), encoding="utf-8")
                summary["scorecard"] = {k: v for k, v in card.items() if k != "labels"}
            (out / "audit.json").write_text(json.dumps(summary, indent=1), encoding="utf-8")
    return summary


async def _load(db: Database, found: Export) -> None:
    """Everything but what comes in day by day."""
    header = found.header

    def write(t: Transaction) -> None:
        for item in found.records.get("setting", []):
            if item["kind"] in SETTINGS:
                t.execute(
                    "INSERT OR REPLACE INTO settings (kind, id, body, updated) VALUES (?, ?, ?, ?)",
                    (item["kind"], item["id"], json.dumps(item["body"]), "replay"),
                )
        location = Location(
            latitude=-45.0 if header.get("south") else 45.0,
            longitude=0.0,
            timezone=header.get("timezone", "UTC"),
        )
        t.execute(
            "INSERT OR REPLACE INTO settings (kind, id, body, updated) VALUES (?, ?, ?, ?)",
            ("location", "", location.model_dump_json(), "replay"),
        )
        t.executemany(
            "INSERT OR REPLACE INTO intervals (instance, series, start, end, value, unit, vat,"
            " status, revision, published, source, why)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [r["row"] for r in found.records.get("interval", [])],
        )
        t.executemany(
            "INSERT OR REPLACE INTO intents (id, body, state, created, ended)"
            " VALUES (?, ?, ?, ?, ?)",
            [
                (r["id"], json.dumps(r["body"]), r["state"], r["created"], r["ended"])
                for r in found.records.get("intent", [])
            ],
        )
        t.executemany(
            "INSERT OR REPLACE INTO levels (id, body) VALUES (?, ?)",
            [(r["id"], json.dumps(r["body"])) for r in found.records.get("level", [])],
        )
        t.execute(
            "CREATE TABLE replay_leads (source TEXT, quantity TEXT, valid REAL, lead INTEGER,"
            " value REAL, made REAL, observed REAL)"
        )
        t.executemany(
            "INSERT INTO replay_leads VALUES (?, ?, ?, ?, ?, ?, ?)",
            [r["row"] for r in found.records.get("forecast_lead", [])],
        )

    await db.run(write)


async def _stage(db: Database, source: Path) -> None:
    """The history, held aside to go in a day at a time."""

    def write(t: Transaction) -> None:
        t.execute(
            "CREATE TABLE replay_history (instance TEXT, point TEXT, t REAL, value REAL,"
            " text TEXT, quality TEXT)"
        )
        batch: list[list[Any]] = []
        for item in _lines(source):
            if item["record"] != "history":
                continue
            batch.append(item["row"])
            if len(batch) >= 10_000:
                t.executemany("INSERT INTO replay_history VALUES (?, ?, ?, ?, ?, ?)", batch)
                batch.clear()
        t.executemany("INSERT INTO replay_history VALUES (?, ?, ?, ?, ?, ?)", batch)
        t.execute("CREATE INDEX replay_history_t ON replay_history (t)")

    await db.run(write)


async def _feed(db: Database, until: float) -> None:
    """What had been recorded, and measured, by `until`."""

    def write(t: Transaction) -> None:
        t.execute(
            "INSERT OR REPLACE INTO history (instance, point, t, value, text, quality)"
            " SELECT instance, point, t, value, text, quality FROM replay_history WHERE t < ?",
            (until,),
        )
        t.execute("DELETE FROM replay_history WHERE t < ?", (until,))
        t.execute(
            "INSERT OR REPLACE INTO forecast_leads (source, quantity, valid, lead, value, made,"
            " observed) SELECT source, quantity, valid, lead, value, made, observed"
            " FROM replay_leads WHERE valid < ?",
            (until,),
        )
        t.execute("DELETE FROM replay_leads WHERE valid < ?", (until,))

    await db.run(write)


def _units(described: dict[str, Described]) -> dict[Key, str | None]:
    return {Key(instance, p.path): p.unit for instance, d in described.items() for p in d.points}


async def _latest(db: Database, units: dict[Key, str | None]) -> dict[Key, Envelope]:
    """Each point's last sample so far, as the core's latest values would hold it."""

    def read(t: Transaction) -> list[tuple[str, str, float, float | None, str | None, str]]:
        return list(
            t.execute(
                "SELECT h.instance, h.point, h.t, h.value, h.text, h.quality FROM history h"
                " JOIN (SELECT instance, point, MAX(t) AS last FROM history"
                " GROUP BY instance, point) m"
                " ON h.instance = m.instance AND h.point = m.point AND h.t = m.last"
            )
        )

    out = {}
    for instance, point, t, value, text, quality in await db.run(read):
        key = Key(instance, point)
        unit = units.get(key, QUANTITIES.get(point.rpartition("/")[2]))
        seen = datetime.fromtimestamp(t, UTC)
        out[key] = Envelope.model_validate(
            {
                "point": point,
                "value": value if value is not None else text,
                "unit": unit if value is not None else None,
                "t_observed": seen,
                "t_received": seen,
                "quality": quality,
                "source": "measured",
            }
        )
    return out


async def _coverage(db: Database) -> dict[str, dict[str, Any]]:
    def read(t: Transaction) -> list[tuple[str, str, float, float, int]]:
        return list(
            t.execute(
                "SELECT instance, point, MIN(t), MAX(t), COUNT(*) FROM history"
                " GROUP BY instance, point ORDER BY instance, point"
            )
        )

    return {
        f"{instance}:{point}": {
            "first": datetime.fromtimestamp(a, UTC).isoformat(),
            "last": datetime.fromtimestamp(b, UTC).isoformat(),
            "samples": n,
            "hours": round((b - a) / 3600, 1),
        }
        for instance, point, a, b, n in await db.run(read)
    }
