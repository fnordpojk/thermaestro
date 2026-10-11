"""A period of a running installation, written out to be replayed elsewhere.

The file is JSON lines, gzip-compressed: a header first, then one record a line, each with
its `record` kind. It holds what the learners need and what was decided:
- each plugin's description of its devices (a unit's serial number left out);
- the settings learning reads: the house's answers, rooms, sensors, the outdoor
  references, names, control, the documented figures, the climate and the weather choice;
  the location's time zone and hemisphere, not its coordinates;
- the history in the period, raw and as 15-minute aggregates;
- the forecasts' and prices' intervals, and the forecasts kept at lead times with what was
  measured then; where a provider gives cloud cover but no sunlight, the sunlight worked
  out from it here, as the coordinates stay out;
- what was decided (and in shadow, would have been), with what the device showed;
- intents, levels, and what had been learned and how the inputs were judged when exported.

Accounts, sessions, tokens, the audit log, secrets and plugins' connection settings
(hosts, ports, keys) aren't in it.
"""

import gzip
import json
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import IO, Any

from ..core.sun import sunlight
from ..store import Database, Location, Transaction
from ..store.database import VERSION

FORMAT = "thermaestro-export"
FORMAT_VERSION = 1
SETTINGS = (
    "home",
    "room",
    "sensor",
    "outdoor",
    "names",
    "control",
    "rated",
    "climate",
    "weather",
)
"""The setting kinds an export carries: none of them holds an address or a secret."""
CLOUD = "cloud_cover"
SUN = "irradiance.global"


def _line(out: IO[str], record: str, **fields: Any) -> None:
    out.write(json.dumps({"record": record, **fields}, separators=(",", ":")) + "\n")


def _described(body: str) -> dict[str, Any]:
    found: dict[str, Any] = json.loads(body)
    for node in found.get("nodes", []):
        identity = node.get("identity")
        if isinstance(identity, dict):
            identity.pop("serial", None)
    return found


def _rows(t: Transaction, sql: str, args: tuple[Any, ...] = ()) -> Iterator[tuple[Any, ...]]:
    yield from t.execute(sql, args)


async def export(db: Database, out: Path, start: float, end: float) -> dict[str, int]:
    """Write [start, end) to `out`; how many records of each kind were written."""
    location = await db.get(Location)

    def write(t: Transaction) -> dict[str, int]:
        counts: dict[str, int] = {}

        def put(f: IO[str], record: str, **fields: Any) -> None:
            _line(f, record, **fields)
            counts[record] = counts.get(record, 0) + 1

        with gzip.open(out, "wt", encoding="utf-8") as f:
            _line(
                f,
                "header",
                format=FORMAT,
                version=FORMAT_VERSION,
                database=VERSION,
                created=datetime.now(UTC).isoformat(),
                start=start,
                end=end,
                timezone=location.timezone if location else "UTC",
                south=bool(location and location.latitude < 0),
            )
            for instance, body in _rows(
                t, "SELECT instance, body FROM described ORDER BY instance"
            ):
                put(f, "described", instance=instance, body=_described(body))
            marks = ",".join("?" * len(SETTINGS))
            for kind, id, body in _rows(
                t,
                f"SELECT kind, id, body FROM settings WHERE kind IN ({marks}) ORDER BY kind, id",  # noqa: S608
                SETTINGS,
            ):
                put(f, "setting", kind=kind, id=id, body=json.loads(body))
            for row in _rows(
                t,
                "SELECT instance, point, t, value, text, quality FROM history"
                " WHERE t >= ? AND t < ? ORDER BY t",
                (start, end),
            ):
                put(f, "history", row=list(row))
            for row in _rows(
                t,
                "SELECT instance, point, slot, min, mean, max, last, n FROM history_15m"
                " WHERE slot >= ? AND slot < ? ORDER BY slot",
                (start, end),
            ):
                put(f, "history_15m", row=list(row))
            offered: set[tuple[str, str]] = set()
            clouds: list[tuple[str, str, float, float, float]] = []
            for row in _rows(
                t,
                "SELECT instance, series, start, end, value, unit, vat, status, revision,"
                " published, source, why FROM intervals WHERE end > ? AND start < ?"
                " ORDER BY start",
                (start - 86_400, end + 86_400),
            ):
                put(f, "interval", row=list(row))
                instance, series = row[0], row[1]
                prefix = series.rpartition("/")[0]
                if series.endswith(SUN):
                    offered.add((instance, prefix))
                elif series.endswith(CLOUD) and location is not None:
                    clouds.append((instance, prefix, row[2], row[3], row[4]))
            for instance, prefix, t0, t1, cloud in clouds:
                if (instance, prefix) in offered:
                    continue
                sun = sunlight(
                    datetime.fromtimestamp(t0, UTC),
                    datetime.fromtimestamp(t1, UTC),
                    cloud,
                    location.latitude if location else 0.0,
                    location.longitude if location else 0.0,
                )
                series = f"{prefix}/{SUN}" if prefix else SUN
                put(
                    f,
                    "interval",
                    row=[
                        instance,
                        series,
                        t0,
                        t1,
                        round(sun, 1),
                        "W/m2",
                        "none",
                        "estimated",
                        0,
                        None,
                        None,
                        "worked out from cloud cover when exported",
                    ],
                )
            for row in _rows(
                t,
                "SELECT source, quantity, valid, lead, value, made, observed FROM forecast_leads"
                " WHERE valid >= ? AND valid < ? ORDER BY valid",
                (start, end),
            ):
                put(f, "forecast_lead", row=list(row))
            for row in _rows(
                t,
                "SELECT a.t, a.lever, a.op, a.params, a.who, a.why, a.mode, a.outcome,"
                " a.detail, f.found FROM acts a LEFT JOIN act_found f ON f.act = a.id"
                " WHERE a.t >= ? AND a.t < ? ORDER BY a.t",
                (start, end),
            ):
                put(f, "act", row=list(row))
            for id, body, state, created, ended in _rows(
                t, "SELECT id, body, state, created, ended FROM intents ORDER BY created"
            ):
                put(
                    f,
                    "intent",
                    id=id,
                    body=json.loads(body),
                    state=state,
                    created=created,
                    ended=ended,
                )
            for id, body in _rows(t, "SELECT id, body FROM levels ORDER BY id"):
                put(f, "level", id=id, body=json.loads(body))
            for key, body in _rows(t, "SELECT key, body FROM learned ORDER BY key"):
                put(f, "learned", key=key, body=json.loads(body))
            for row in _rows(
                t,
                "SELECT instance, point, verdict, why, args, checked FROM input_checks"
                " ORDER BY instance, point",
            ):
                put(f, "input_check", row=list(row))
        return counts

    return await db.run(write)
