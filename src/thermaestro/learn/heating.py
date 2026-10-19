"""The house's learning, per climate system, day by day.

Once a day is over, its hours are read and kept as events: the rooms' mean temperature
(the rooms with a sensor in the climate system), the outdoor temperature (the outdoor
reference, else the pump's own sensor), the water (the mean of the system's supply and
return), the sun and the wind as forecast for the house, and why an hour is left out: a
fireplace on, a room aired (a window open, or a fall of a degree within ten minutes), or
the pump's heating stop holding heating off. Kept as hours, they outlive the raw history
and the forecasts (a week).

Then the house is fitted again over the last four weeks of hours, and judged on its last
two days (`learn.house`). While heating is stopped, nothing new can be learned: what was
learned is kept, frozen.

A climate system is learned only once its supply, an outdoor temperature and at least one
of its rooms are usable (the input checks).
"""

import asyncio
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from functools import partial
from typing import TYPE_CHECKING, Any

from .. import clock
from ..core.host import PluginHost
from ..core.sensors import SITE
from ..core.values import Key, Sample, Values
from ..intents.calendar import Calendar
from ..intents.entry import RESPONSE_H
from ..store import Database, Home, Room, Sensor, Transaction
from . import house, pump
from .hotwater import house as house_days
from .hygiene import intervals
from .inputs import intersect, seconds, spans
from .model import Learned, State, mark
from .store import Learnings
from .tank import bounds, first_day

if TYPE_CHECKING:
    from ..core.weather import Weather

log = logging.getLogger(__name__)

MODEL = "house.model"
TAU = "house.tau_h"
RESPONSE = "house.response_h"
PUMP = "pump.response"
WATER = "pump.water"
DAYS = 28
"""The days of hours fitted over."""
POSTPONE_S = 2 * 86_400.0
"""How long a day whose weather couldn't be read waits to be read again before it is kept
without it."""
FIREPLACE_S = 6 * 3600.0
AIRED_DROP = 1.0
AIRED_WITHIN_S = 600.0
HOLD = {
    "room": 6 * 3600.0,
    "outdoor": 3 * 3600.0,
    "water": 1800.0,
    "stop": 6 * 3600.0,
    "offset": 24 * 3600.0,
}
"""How long a reading holds with nothing after it: a room sensor may report only on a
change, the pump's values come often."""
WATER_COVERED = 0.5
"""A hot-water charge takes the water from the climate system for a while: half the hour
is enough to know it."""
FROZEN_SHARE = 0.5
TAU_START = {"poorly_insulated": 20.0, "average": 40.0, "well_insulated": 70.0, "low_energy": 120.0}
"""A start for the house's time constant, hours, from setup's answer, until it is learned."""

NOT_FITTED = mark("Not fitted yet.")
TESTING = mark("Judged once there are two more days to test it on.")
FROZEN = mark("Heating is held off by the pump's heating stop: kept as learned until it heats.")
MISSING = mark("Not learned: %(what)s isn't usable on this pump yet.")
WHAT = {
    "water": mark("the climate system's supply temperature"),
    "room": mark("a room temperature in this climate system"),
    "outdoor": mark("an outdoor temperature"),
}


@dataclass(frozen=True, slots=True)
class SystemKeys:
    scope: str
    supply: Key
    back: Key
    outdoor: list[Key]
    stop: Key
    mean_out: Key
    rooms: list[str]
    """Room ids with a temperature sensor."""
    demand: Key | None = None
    compressor: Key | None = None
    offset: Key | None = None
    """The point the heating offset is read back by."""
    target: Key | None = None
    """The supply temperature the pump's curve calculates, where it reports one."""


class Heating:
    def __init__(
        self,
        db: Database,
        values: Values,
        host: PluginHost,
        learnings: Learnings,
        usable: Callable[[Key], bool],
        weather: "Weather | None" = None,
    ) -> None:
        self._db = db
        self._values = values
        self._host = host
        self._learnings = learnings
        self._usable = usable
        self._weather = weather

    async def systems(self) -> list[SystemKeys]:
        rooms = await self._db.all(Room)
        sensed = {
            s.room
            for s in (await self._db.all(Sensor)).values()
            if s.room is not None and s.quantity == "temperature"
        }
        out = []
        for instance_id, instance in sorted(self._host.instances.items()):
            described = instance.described
            if described is None:
                continue
            units = [n.path for n in described.nodes if n.kind == "unit"]
            for node in described.nodes:
                if node.kind != "climate_system":
                    continue
                unit = next((u for u in units if node.path.startswith(f"{u}/")), None)
                if unit is None:
                    continue
                scope = f"{instance_id}:{node.path}"
                kinds = {n.path: n.kind for n in described.nodes}
                compressor = next(
                    (
                        Key(instance_id, p.path)
                        for p in described.points
                        if p.path.startswith(f"{unit}/")
                        and p.path.endswith("/state")
                        and kinds.get(p.path.rpartition("/")[0]) == "compressor"
                    ),
                    None,
                )
                lever = next(
                    (
                        lv
                        for lv in getattr(described, "levers", ())
                        if lv.path == f"{node.path}/heating.offset"
                    ),
                    None,
                )
                point = lever.verify.point if lever is not None else None
                target = f"{node.path}/supply.temp.target"
                described_paths = {p.path for p in described.points}
                out.append(
                    SystemKeys(
                        scope,
                        Key(instance_id, f"{node.path}/supply.temp"),
                        Key(instance_id, f"{node.path}/return.temp"),
                        [
                            Key(SITE, "outdoor/temperature"),
                            Key(instance_id, f"{unit}/outdoor.temp"),
                        ],
                        Key(instance_id, f"{unit}/heating.stop_temp"),
                        Key(instance_id, f"{unit}/outdoor.temp.mean"),
                        sorted(
                            id
                            for id, room in rooms.items()
                            if room.climate_system == scope and id in sensed
                        ),
                        Key(instance_id, f"{unit}/demand"),
                        compressor,
                        Key(instance_id, point) if point else None,
                        Key(instance_id, target) if target in described_paths else None,
                    )
                )
        return out

    async def run(self, now: float | None = None) -> None:
        now = clock.time() if now is None else now
        calendar, _ = await house_days(self._db)
        home = await self._db.get(Home) or Home()
        for keys in await self.systems():
            try:
                await self._learn(keys, calendar, home, now)
            except Exception:
                log.exception("learning the house of %s failed; trying again later", keys.scope)

    def _inputs(self, keys: SystemKeys) -> tuple[Key | None, list[Key], str | None]:
        outdoor = next((k for k in keys.outdoor if self._usable(k)), None)
        rooms = [k for id in keys.rooms if self._usable(k := Key(SITE, f"room.{id}/temperature"))]
        missing = "water" if not self._usable(keys.supply) else None
        return outdoor, rooms, missing or ("outdoor" if outdoor is None else None)

    async def _learn(self, keys: SystemKeys, calendar: Calendar, home: Home, now: float) -> None:
        outdoor, rooms, missing = self._inputs(keys)
        if missing is not None or outdoor is None:
            await self._note(keys.scope, WHAT[missing or "outdoor"])
            return
        zone = calendar.zone
        first = await self._first_raw(keys.supply)
        day = first_day(first if first is not None else now, await self._last_day(keys.scope), zone)
        read = False
        while first is not None and bounds(day, zone)[1] <= now - 3600:
            hours, weather = await self._day(keys, outdoor, rooms, day, calendar)
            if not weather and now - bounds(day, zone)[1] < POSTPONE_S:
                break  # the weather couldn't be read: read again later, not as no sun
            # Kept even with nothing in it, so it isn't read again.
            body = {"day": day.isoformat(), "hours": [_hour_body(h) for h in hours]}
            start = bounds(day, zone)[0]
            await self._db.run(partial(_keep, scope=keys.scope, start=start, body=body))
            day += timedelta(days=1)
            read = True
        # Fitted again only with a new day: it takes seconds.
        if read or self._learnings.get(MODEL, keys.scope) is None:
            await self._judge(keys, home, now, bool(rooms))

    async def _day(
        self, keys: SystemKeys, outdoor: Key, rooms: list[Key], day: date, calendar: Calendar
    ) -> tuple[list[house.Hour], bool]:
        """The day's hours; and whether the weather could be read for them."""
        start, end = bounds(day, calendar.zone)
        count = int((end - start) // 3600)
        before = start - HOLD["room"]

        async def hourly(key: Key, hold: float, covered: float = 0.95) -> list[float | None]:
            samples = await self._values.history(key, before, end)
            found = intervals(samples, start, end, 3600, hold_s=hold, coverage=covered)
            return [i.mean if i is not None else None for i in found]

        room_means = [await hourly(k, HOLD["room"]) for k in rooms]
        room = [_mean([r[i] for r in room_means]) for i in range(count)]
        out = await hourly(outdoor, HOLD["outdoor"])
        supply = await hourly(keys.supply, HOLD["water"], WATER_COVERED)
        # The return as read, usable yet or not: which water the house is learned from is
        # decided when it is judged, the same for every day.
        back = await hourly(keys.back, HOLD["water"], WATER_COVERED)
        water = [
            (s + b) / 2 if s is not None and b is not None else s
            for s, b in zip(supply, back, strict=True)
        ]
        stop = await hourly(keys.stop, HOLD["stop"])
        mean_out = await hourly(keys.mean_out, HOLD["stop"])
        offset = (
            await hourly(keys.offset, HOLD["offset"], 0.5)
            if keys.offset is not None
            else [None] * count
        )
        target = (
            await hourly(keys.target, HOLD["water"], WATER_COVERED)
            if keys.target is not None and self._usable(keys.target)
            else [None] * count
        )
        share = await self._heating_share(keys, start, count)
        sun = await self._forecast("irradiance.global", start, count)
        wind = await self._forecast("wind_speed", start, count)
        weather = sun is not None and wind is not None
        sun = sun if sun is not None else [None] * count
        wind = wind if wind is not None else [None] * count
        fires = await self._fireplaces(start, end)
        aired = await self._aired(keys, rooms, start, end)
        hours = []
        for i in range(count):
            t = start + i * 3600
            skip = None
            if any(f0 < t + 3600 and t < f1 for f0, f1 in fires):
                skip = "fireplace"
            elif any(t <= a < t + 3600 for a in aired):
                skip = "airing"
            elif (stopping := stop[i]) is not None and (mean := mean_out[i]) is not None:
                skip = "heating_off" if mean >= stopping else None
            hours.append(
                house.Hour(
                    t,
                    room[i],
                    out[i],
                    water[i],
                    sun[i],
                    wind[i],
                    skip,
                    supply[i],
                    offset[i],
                    share[i],
                    target[i],
                    back[i],
                )
            )
        return hours, weather

    async def _heating_share(
        self, keys: SystemKeys, start: float, count: int
    ) -> list[float | None]:
        """The share of each hour the compressor ran with the demand on heating."""
        if keys.demand is None or keys.compressor is None:
            return [None] * count
        end = start + count * 3600
        demand = await self._values.history(keys.demand, start - 3600, end)
        compressor = await self._values.history(keys.compressor, start - 3600, end)
        if not demand or not compressor:
            return [None] * count
        heating = intersect(
            spans(compressor, end, lambda s: s.text == "running"),
            spans(demand, end, lambda s: s.text == "heating"),
        )
        return [
            seconds(intersect(heating, [(start + i * 3600, start + (i + 1) * 3600)])) / 3600
            for i in range(count)
        ]

    async def _forecast(self, quantity: str, start: float, count: int) -> list[float | None] | None:
        """The forecast hour by hour; None where it couldn't be read (as against there
        being no weather to read: then it is all unknown)."""
        if self._weather is None:
            return [None] * count
        from ..core.weather import INSTANT, value_at

        first = datetime.fromtimestamp(start, UTC)
        try:
            found = await self._weather.forecast(quantity, first, first + timedelta(hours=count))
        except Exception:
            log.exception("the %s forecast couldn't be read for learning", quantity)
            return None
        return [
            value_at(found.spans, first + timedelta(hours=i, minutes=30), quantity in INSTANT)
            for i in range(count)
        ]

    async def _fireplaces(self, start: float, end: float) -> list[tuple[float, float]]:
        def read(t: Transaction) -> list[tuple[float, float]]:
            rows = t.execute(
                "SELECT body, created, ended FROM intents WHERE created < ?"
                " AND (ended IS NULL OR ended > ?)",
                (end, start - FIREPLACE_S),
            )
            found = []
            for body, created, ended in rows:
                if json.loads(body).get("kind") == "fireplace":
                    found.append((created, ended if ended is not None else created + FIREPLACE_S))
            return found

        return await self._db.run(read)

    async def _aired(
        self, keys: SystemKeys, rooms: list[Key], start: float, end: float
    ) -> list[float]:
        """When a room was aired: a window open, or a fall of a degree within ten minutes."""
        found: list[float] = []
        for key in rooms:
            samples = await self._values.history(key, start - AIRED_WITHIN_S, end)
            found += _drops(samples)
            window = Key(SITE, key.point.replace("/temperature", "/window.open"))
            for s in await self._values.history(window, start, end):
                if s.quality == "good" and s.value:
                    found.append(s.t)
        return found

    async def _judge(self, keys: SystemKeys, home: Home, now: float, rooms: bool) -> None:
        days = await self._events(keys.scope, now - DAYS * 86_400)
        back = self._usable(keys.back)
        hours = [_hour(h, back) for _, body in days for h in body["hours"]]
        hours.sort(key=lambda h: h.t)
        if not hours:
            if not rooms:
                await self._note(keys.scope, WHAT["room"])
            return
        # The pump's curve needs no room; the house does.
        await self._pump(keys.scope, hours, now)
        if not rooms:
            await self._note(keys.scope, WHAT["room"])
            return
        recent = [h for h in hours if h.t >= now - 3 * 86_400]
        off = sum(1 for h in recent if h.skip == "heating_off")
        before = self._learnings.get(MODEL, keys.scope)
        if recent and off >= FROZEN_SHARE * len(recent) and before is not None:
            for name in (MODEL, TAU, RESPONSE):
                found = self._learnings.get(name, keys.scope)
                # Only what had proved itself is kept in use; anything else waits as it is.
                if found is not None and found.value is not None and found.state == "proved":
                    update = {"state": "frozen", "why": FROZEN, "args": {}}
                    await self._learnings.put(found.model_copy(update=update))
            return
        # Seconds of arithmetic: off the event loop.
        test_from = now - house.TEST_HOURS * 3600
        whole, verdict, why, args = await asyncio.to_thread(house.judge, hours, test_from)
        emitter = home.emitters.get(keys.scope, "unknown")
        starts = {
            TAU: TAU_START.get(home.house),
            RESPONSE: RESPONSE_H.get(emitter, RESPONSE_H["unknown"]),
        }
        kept = sum(1 for h in hours if h.room is not None and not h.skip)
        if whole is None:
            for name in (MODEL, TAU, RESPONSE):
                found = self._learnings.get(name, keys.scope)
                if found is not None and found.state in ("proved", "frozen"):
                    # Too few hours yet since heating came back: what proved itself stays in
                    # use until there are enough to judge it again.
                    continue
                await self._learnings.put(
                    Learned(
                        name=name,
                        scope=keys.scope,
                        kind="model",
                        value=found.value if found else None,
                        unit=_unit(name),
                        state="learning",
                        why=why or NOT_FITTED,
                        args=args,
                        starting=starts.get(name),
                        n=kept,
                        since=hours[0].t,
                    )
                )
            return
        state: State = "proved" if verdict is not None and verdict.proved else "learning"
        reason = verdict.why if verdict is not None else TESTING
        reason_args: dict[str, float | int | str] = dict(verdict.args) if verdict else {}
        values: dict[str, Any] = {
            MODEL: whole.as_value(),
            TAU: round(whole.tau_h, 1) if whole.tau_h is not None else None,
            RESPONSE: float(whole.lag),
        }
        for name, value in values.items():
            await self._learnings.put(
                Learned(
                    name=name,
                    scope=keys.scope,
                    kind="model",
                    value=value,
                    unit=_unit(name),
                    state=state,
                    why=reason,
                    args=reason_args,
                    starting=starts.get(name),
                    n=whole.rows,
                    since=hours[0].t,
                )
            )

    async def _pump(self, scope: str, hours: list[house.Hour], now: float) -> None:
        """How the supply follows the outdoor temperature and the offset, and the water."""
        await self._water(scope, hours, now)
        found, verdict, why, args = pump.judge(hours, now - pump.TEST_HOURS * 3600)
        before = self._learnings.get(PUMP, scope)
        if found is None:
            if before is not None and before.value is not None:
                return  # kept as it was learned
            await self._learnings.put(
                Learned(name=PUMP, scope=scope, kind="model", state="learning", why=why, args=args)
            )
            return
        state: State = "proved" if verdict is not None and verdict.proved else "learning"
        await self._learnings.put(
            Learned(
                name=PUMP,
                scope=scope,
                kind="model",
                value=found.as_value(),
                state=state,
                why=verdict.why if verdict is not None else TESTING,
                args=dict(verdict.args) if verdict is not None else {},
                n=found.rows,
                since=before.since if before and before.since else hours[0].t,
            )
        )

    async def _water(self, scope: str, hours: list[house.Hour], now: float) -> None:
        found, verdict, why, args = await asyncio.to_thread(
            pump.judge_water, hours, now - pump.TEST_HOURS * 3600
        )
        before = self._learnings.get(WATER, scope)
        if found is None:
            if before is not None and before.value is not None:
                return
            await self._learnings.put(
                Learned(name=WATER, scope=scope, kind="model", state="learning", why=why, args=args)
            )
            return
        state: State = "proved" if verdict is not None and verdict.proved else "learning"
        await self._learnings.put(
            Learned(
                name=WATER,
                scope=scope,
                kind="model",
                value=found.as_value(),
                state=state,
                why=verdict.why if verdict is not None else TESTING,
                args=dict(verdict.args) if verdict is not None else {},
                n=found.rows,
                since=before.since if before and before.since else hours[0].t,
            )
        )

    async def _note(self, scope: str, what: str) -> None:
        found = self._learnings.get(MODEL, scope)
        if found is not None and found.value is not None:
            return
        await self._learnings.put(
            Learned(
                name=MODEL,
                scope=scope,
                kind="model",
                state="starting",
                why=MISSING,
                args={"what": what},
            )
        )

    async def _first_raw(self, key: Key) -> float | None:
        def read(t: Any) -> float | None:
            row = t.execute(
                "SELECT MIN(t) FROM history WHERE instance = ? AND point = ?",
                (key.instance, key.point),
            ).fetchone()
            return None if row is None else row[0]

        return await self._db.run(read)

    async def _last_day(self, scope: str) -> date | None:
        found = await self._events(scope, 0.0)
        if not found:
            return None
        return date.fromisoformat(found[-1][1]["day"])

    async def _events(self, scope: str, since: float) -> list[tuple[float, dict[str, Any]]]:
        def read(t: Transaction) -> list[tuple[float, dict[str, Any]]]:
            rows = t.execute(
                "SELECT t, body FROM learn_events WHERE scope = ? AND kind = 'house_day'"
                " AND t >= ? ORDER BY t",
                (scope, since),
            )
            return [(at, json.loads(body)) for at, body in rows]

        return await self._db.run(read)


def _unit(name: str) -> str | None:
    return "h" if name in (TAU, RESPONSE) else None


def _mean(values: list[float | None]) -> float | None:
    known = [v for v in values if v is not None]
    return sum(known) / len(known) if known else None


def _drops(samples: list[Sample]) -> list[float]:
    """When a room's temperature fell a degree or more within ten minutes."""
    found = []
    good = [s for s in samples if s.quality == "good" and s.value is not None]
    j = 0
    for i, s in enumerate(good):
        while good[j].t < s.t - AIRED_WITHIN_S:
            j += 1
        if any((good[k].value or 0.0) - (s.value or 0.0) >= AIRED_DROP for k in range(j, i)):
            found.append(s.t)
    return found


def _hour_body(h: house.Hour) -> dict[str, Any]:
    return {
        "t": h.t,
        "room": _round(h.room),
        "outdoor": _round(h.outdoor),
        "water": _round(h.water),
        "sun": _round(h.sun),
        "wind": _round(h.wind),
        "skip": h.skip,
        "supply": _round(h.supply),
        "offset": _round(h.offset),
        "heating": _round(h.heating),
        "target": _round(h.target),
        "back": _round(h.back),
    }


def _hour(body: dict[str, Any], back: bool = True) -> house.Hour:
    """An hour as kept. Where its return was kept too, its water is the mean of supply and
    return while the return is usable (an hour without a return isn't used then), and the
    supply alone while it isn't: every day the same."""
    water = body.get("water")
    if "back" in body:
        supply, ret = body.get("supply"), body.get("back")
        if not back:
            water = supply
        else:
            water = (supply + ret) / 2 if supply is not None and ret is not None else None
    return house.Hour(
        float(body["t"]),
        body.get("room"),
        body.get("outdoor"),
        water,
        body.get("sun"),
        body.get("wind"),
        body.get("skip"),
        body.get("supply"),
        body.get("offset"),
        body.get("heating"),
        body.get("target"),
        body.get("back"),
    )


def _round(v: float | None) -> float | None:
    return None if v is None else round(v, 3)


def _keep(t: Transaction, scope: str, start: float, body: dict[str, Any]) -> None:
    t.execute(
        "INSERT OR REPLACE INTO learn_events (scope, kind, t, body) VALUES (?, 'house_day', ?, ?)",
        (scope, start, json.dumps(body)),
    )
