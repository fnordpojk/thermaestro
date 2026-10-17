"""The daily summary, made at 07:00 for the day before, midnight to midnight in the house's
time:

- what was met and what was missed: each intent that ended that day, with what came of it;
  each room's minutes below its band; the hot water's minutes below its floor;
- a line per temporary intent that ran that day: the pump's electricity while it ran, and
  what it cost, from the pump's own meter where there is one, else from the compressor's
  learned draw for the time it ran and the addition's power;
- the day's electricity and cost, the writes Thermaestro asked for, and the compressor's
  starts.

What a temporary intent used is what the pump used while it ran, not what it used more than
without it: that needs a baseline, the pump alone on the same days, which comes with
replaying recordings. Each summary is kept, shown on the Events page, and told as an event.
A day nothing was seen in, before Thermaestro ran, isn't summed up.
"""

import json
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, tzinfo
from typing import Any

from ..core.events import Events, Raise, mark
from ..core.executor import COUNTED, split
from ..core.values import Key, Values
from ..intents import Intent
from ..intents.entry import named
from ..intents.model import TEMPORARY
from ..learn import Learnings
from ..learn.electric import counter_kwh, runs, starts
from ..learn.energy import DRAW
from ..learn.meters import pump_meters
from ..learn.tank import Series
from ..store import Database, PriceLayer
from .model import Price
from .optimizer import _running, held_mean
from .watch import MISSED_S

AT = time(7)
"""When the day before is summed up, in the house's time."""
SLOT_S = 900.0
KEEP_DAYS = 90
READY = mark("The summary of %(day)s is ready")


@dataclass(frozen=True)
class Pump:
    """What the summary reads of a pump unit."""

    unit: str
    """`instance:path`."""
    compressor: Key | None


class Summaries:
    def __init__(
        self,
        db: Database,
        values: Values,
        events: Events | None,
        learnings: Learnings | None,
        pumps: Callable[[], Sequence[Pump]],
        prices: Callable[[datetime], Awaitable[Sequence[Price]]],
    ) -> None:
        self._db = db
        self._values = values
        self._events = events
        self._learnings = learnings
        self._pumps = pumps
        self._prices = prices
        self._looked: date | None = None
        """The last day made or passed over: once a day, not every check."""

    async def due(self, now: datetime, zone: tzinfo) -> dict[str, Any] | None:
        """The day before's summary, made if it is 07:00 or later and it isn't yet."""
        local = now.astimezone(zone)
        if local.time() < AT:
            return None
        day = local.date() - timedelta(days=1)
        if day == self._looked:
            return None
        self._looked = day
        if await self.get(day) is not None or not await self._seen(day, zone):
            return None
        made = await self.make(day, zone, now)
        await self._keep(day, made, now.timestamp())
        if self._events is not None:
            what = Raise("summary.daily", "house", READY, {"day": day.isoformat()}, "household")
            await self._events.moment(f"summary.daily:{day.isoformat()}", what)
        return made

    async def get(self, day: date) -> dict[str, Any] | None:
        row = await self._db.run(
            lambda t: t.execute(
                "SELECT body FROM summaries WHERE day = ?", (day.isoformat(),)
            ).fetchone()
        )
        return None if row is None else dict(json.loads(row[0]))

    async def recent(self, days: int) -> list[dict[str, Any]]:
        """The last `days` summaries, newest first."""
        rows = await self._db.run(
            lambda t: t.execute(
                "SELECT body FROM summaries ORDER BY day DESC LIMIT ?", (days,)
            ).fetchall()
        )
        return [dict(json.loads(r[0])) for r in rows]

    async def _seen(self, day: date, zone: tzinfo) -> bool:
        """Whether anything was kept from before the day ended."""
        t1 = datetime.combine(day + timedelta(days=1), time(0), zone).timestamp()
        row = await self._db.run(
            lambda t: t.execute("SELECT 1 FROM history WHERE t < ? LIMIT 1", (t1,)).fetchone()
        )
        return row is not None

    async def _keep(self, day: date, body: dict[str, Any], now: float) -> None:
        def write(t: Any) -> None:
            t.execute(
                "INSERT OR REPLACE INTO summaries (day, made, body) VALUES (?, ?, ?)",
                (day.isoformat(), now, json.dumps(body)),
            )
            oldest = (day - timedelta(days=KEEP_DAYS)).isoformat()
            t.execute("DELETE FROM summaries WHERE day < ?", (oldest,))

        await self._db.run(write)

    # --- making one ----------------------------------------------------------------------------

    async def make(self, day: date, zone: tzinfo, now: datetime) -> dict[str, Any]:
        t0 = datetime.combine(day, time(0), zone).timestamp()
        t1 = datetime.combine(day + timedelta(days=1), time(0), zone).timestamp()
        slots = [t0 + SLOT_S * i for i in range(int((t1 - t0) // SLOT_S))]
        kwh = await self._kwh(slots)
        prices = {p.start.timestamp(): p.value for p in await self._prices(now)}
        cost = [k * prices[s] if s in prices else None for s, k in zip(slots, kwh, strict=True)]
        priced = [c for c in cost if c is not None]
        ended, ran = await self._intents(t0, t1)
        layers = await self._db.all(PriceLayer)
        currency = next(
            (u.split("/")[0] for u in (x.unit for x in layers.values()) if u and "/" in u), None
        )
        return {
            "day": day.isoformat(),
            "ended": [_ended(i) for i in ended],
            "rooms": await self._below("comfort.missed:", t0, t1, "room", MISSED_S),
            "floors": await self._below("floor.broken:", t0, t1, None, 0.0),
            "temporary": [
                {
                    "kind": i.kind,
                    "name": named(i),
                    "kwh": round(_within(slots, kwh, a, b), 1),
                    "cost": _priced(slots, cost, a, b),
                }
                for i, a, b in ran
            ],
            "kwh": round(sum(kwh), 1),
            "cost": round(sum(priced), 2) if priced else None,
            "currency": currency,
            "writes": await self._writes(t0, t1),
            "starts": await self._starts(t0, t1),
        }

    async def _kwh(self, slots: list[float]) -> list[float]:
        """What the pumps drew in each slot, kWh."""
        out = [0.0] * len(slots)
        if not slots:
            return out
        end = slots[-1] + SLOT_S
        meters = await pump_meters(self._db)
        for pump in self._pumps():
            meter = meters.get(pump.unit)
            if meter is not None:
                counter = Series(await self._values.history(meter, slots[0] - 3600, end))
                for i, s in enumerate(slots):
                    out[i] += counter_kwh(counter, s, s + SLOT_S) or 0.0
                continue
            draws = self._learnings.used(DRAW, pump.unit) if self._learnings else None
            if not isinstance(draws, Mapping) or pump.compressor is None:
                continue
            heating = float(draws.get("heating") or 0.0)
            dhw = float(draws.get("dhw") or heating)
            instance, path = split(pump.unit)
            ran = await self._values.history(pump.compressor, slots[0] - 3600, end)
            demand = await self._values.history(
                Key(instance, f"{path}/demand"), slots[0] - 3600, end
            )
            added = await self._values.history(
                Key(instance, f"{path}/addition/power"), slots[0] - 3600, end
            )
            for i, s in enumerate(slots):
                running = held_mean(ran, s, s + SLOT_S, _running) or 0.0
                charging = held_mean(demand, s, s + SLOT_S, _charging) or 0.0
                kw = running * ((1 - charging) * heating + charging * dhw)
                kw += held_mean(added, s, s + SLOT_S) or 0.0
                out[i] += kw * SLOT_S / 3600
        return out

    async def _intents(
        self, t0: float, t1: float
    ) -> tuple[list[Intent], list[tuple[Intent, float, float]]]:
        """Those that ended in the day; and the temporary ones that ran in it, with from
        when to when."""
        rows = await self._db.run(
            lambda t: t.execute(
                "SELECT body, ended FROM intents WHERE ended IS NULL OR ended >= ?", (t0,)
            ).fetchall()
        )
        ended, ran = [], []
        for body, stopped in rows:
            intent = Intent.model_validate_json(body)
            if stopped is not None and t0 <= stopped < t1:
                ended.append(intent)
            if intent.kind not in TEMPORARY or intent.state in ("received", "rejected"):
                continue
            start = (intent.validity.starts or intent.created).timestamp()
            a, b = max(start, t0), min(stopped if stopped is not None else t1, t1)
            if a < b:
                ran.append((intent, a, b))
        return ended, ran

    async def _below(
        self, prefix: str, t0: float, t1: float, name: str | None, before_s: float
    ) -> list[dict[str, Any]]:
        """Minutes each scope's condition held in the day, counted from `before_s` before
        it was raised (a room's half hour)."""
        if self._events is None:
            return []
        minutes: dict[str, float] = {}
        for e in await self._events.since(t0, prefix):
            a = max(e.raised - before_s, t0)
            b = min(e.cleared if e.cleared is not None else t1, t1)
            if a < b:
                key = str(e.args.get(name, e.scope)) if name else e.scope
                minutes[key] = minutes.get(key, 0.0) + (b - a) / 60
        return [{"name": k, "minutes": round(v)} for k, v in sorted(minutes.items())]

    async def _writes(self, t0: float, t1: float) -> int:
        marks = ",".join("?" * len(COUNTED))
        row = await self._db.run(
            lambda t: t.execute(
                f"SELECT COUNT(*) FROM acts WHERE who = 'planner' AND t >= ? AND t < ?"  # noqa: S608 - marks, not input
                f" AND outcome IN ({marks})",
                (t0, t1, *sorted(COUNTED)),
            ).fetchone()
        )
        return int(row[0])

    async def _starts(self, t0: float, t1: float) -> int | None:
        found = None
        for pump in self._pumps():
            if pump.compressor is None:
                continue
            samples = await self._values.history(pump.compressor, t0 - 3600, t1)
            found = (found or 0) + starts(runs(samples, t1), t0, t1)
        return found


def _charging(s: Any) -> float | None:
    return None if s.text is None else float(s.text == "dhw")


def _ended(intent: Intent) -> dict[str, Any]:
    return {
        "kind": intent.kind,
        "name": named(intent),
        "state": intent.state,
        "why": intent.why,
        "why_message": intent.why_message,
        "why_args": intent.why_args,
    }


def _within(slots: list[float], values: list[float], a: float, b: float) -> float:
    total = 0.0
    for s, v in zip(slots, values, strict=True):
        share = max(0.0, min(b, s + SLOT_S) - max(a, s)) / SLOT_S
        total += v * share
    return total


def _priced(slots: list[float], cost: list[float | None], a: float, b: float) -> float | None:
    known = [(s, c) for s, c in zip(slots, cost, strict=True) if c is not None]
    if not known:
        return None
    return round(_within([s for s, _ in known], [c for _, c in known], a, b), 2)
