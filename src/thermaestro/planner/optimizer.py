"""The optimizer in the running core: from what the planner gathered and what has been
learned, the problem over the next 48 hours; the plan; and what it asks of the levers.

A goal is planned here only where what it rests on is learned and proved (or pinned):
- a climate system: its house model, how its water follows the offset, its emitters'
  output, and a room with a fresh temperature; its COP and draw learned or documented;
- a tank: its standing loss and its week, and its volume;
- a pool: how fast it warms and cools.

The rules plan whatever isn't covered. The weather is the forecast, its learned bias
taken off; the source the brine as it came in over the last hour (or the forecast outdoor
temperature, for a pump without brine); the power limit the one in force now, and the
household's own load that of the last hour, for the whole horizon. What was measured is
read only up to the last minute surely written, so that shadow plans as control does.
"""

import asyncio
import logging
import math
import statistics
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from ..cap.model import Rating
from ..core.executor import COUNTED, split
from ..core.gridrules import GridLimits
from ..core.host import PluginHost
from ..core.values import Key, Sample, Values
from ..intents.resolve import Resolver
from ..learn import Learnings
from ..learn.efficiency import DHW, HEATING
from ..learn.emitters import EMITTERS, STORED
from ..learn.energy import DRAW, STARTS
from ..learn.forecast import correction
from ..learn.heating import MODEL, WATER
from ..learn.hotwater import CEILING
from ..learn.house import Fit
from ..learn.model import mark
from ..learn.pool import POOL
from ..learn.pump import Water
from ..optimize import (
    Addition,
    Band,
    Deadline,
    Plan,
    Pool,
    Problem,
    Room,
    Slot,
    System,
    Tank,
    levers,
)
from ..optimize import plan as solve
from ..store import Database, Rated, Transaction
from . import rules
from .model import Decision, Situation

if TYPE_CHECKING:
    from ..core.weather import Weather

log = logging.getLogger(__name__)

HORIZON_H = 48
SLOTS = 4 * HORIZON_H
TIME_LIMIT_S = 30.0
WATER_KJ_L_K = 4.186
MUST_MARGIN = 1.0
"""A `must` hot-water deadline is planned this much warmer: what is drawn isn't known."""
ADDITION_KW = 3.0
"""The addition's power where the pump doesn't say."""
BEYOND = 1.0
"""How far past the offsets the house was learned on the plan may go, in steps."""
DRIFT_GAIN = 0.15
"""The share of an hour's miss carried into the drift."""
DRIFT_MOST = 1.0
"""The most drift carried, °C an hour."""
MARGIN_ERRORS = 2.0
MARGIN_LEAST, MARGIN_MOST = 0.25, 0.75
"""The margin kept inside each band, °C: twice the house model's own mean miss six hours
ahead, within these."""

WATER_UNPROVED = mark("how its water follows the offset hasn't proved itself")
POOL_UNPROVED = mark("how it warms and cools hasn't proved itself")
NO_COP = mark("its COP or the compressor's draw isn't known")
HOUSE_UNPROVED = mark("its house model hasn't proved itself")
NO_OFFSET = mark("its offset isn't a lever in use")
TANK_UNPROVED = mark("its standing loss and its week haven't proved themselves")
NO_VOLUME = mark("its volume isn't known")
NO_BAND = mark("it has no band")
NO_TANK_LEVER = mark("neither its block nor its boost is a lever in use")
NO_POOL_LEVER = mark("none of its levers is in use")
NO_ROOM = mark("no room in it has a fresh temperature")
NO_DRAW = mark("the compressor's draw isn't known")
HEATING_OFF = mark("the pump's heating stop has heating off")
EMITTERS_UNPROVED = mark("what its emitters give hasn't proved itself")
NOTHING = mark("nothing it could plan yet: every goal is with the rules")


def _numeric(s: Sample) -> float | None:
    return s.value


def held_mean(
    samples: Sequence[Sample],
    start: float,
    end: float,
    of: Callable[[Sample], float | None] = _numeric,
) -> float | None:
    """The mean from `start` to `end`, each good reading held until the next reading: what
    is kept is a point's changes, and a mean of those would weigh a reading by how often the
    point moved, not by how long it held. A reading that isn't good ends the one before it,
    as the supply's does while a charge takes the water: what the water was then isn't
    known, and the heating's water before it doesn't stand for it."""
    rows = [(s.t, of(s) if s.quality == "good" else None) for s in samples]
    total = held = 0.0
    for i, (t, value) in enumerate(rows):
        if value is None:
            continue
        until = rows[i + 1][0] if i + 1 < len(rows) else end
        span = min(until, end) - max(t, start)
        if span > 0:
            total += value * span
            held += span
    return total / held if held > 0 else None


def _running(s: Sample) -> float | None:
    """A compressor's state as the share of the time it ran."""
    return None if s.text is None else float(s.text == "running")


@dataclass(frozen=True)
class RoomGoal:
    """What a room's intents ask of it, hour by hour, as they ask it: the band's low edge
    without the margin the plan keeps inside it, and which intents set it."""

    system: str
    name: str
    low: tuple[float | None, ...]
    by: tuple[tuple[str, ...], ...]
    reach_h: float
    """How long its heat takes to show: a band can't be met sooner."""


@dataclass(frozen=True)
class DeadlineGoal:
    """A hot-water deadline as its intent asks it, without the margin a `must` is planned
    with."""

    by: str
    scope: str
    slot: int
    at_least: float
    strength: str


@dataclass
class Watched:
    """What each planned goal asks, for judging how its intent stands in the plan."""

    rooms: dict[str, RoomGoal] = field(default_factory=dict)
    deadlines: list[DeadlineGoal] = field(default_factory=list)


@dataclass
class Optimized:
    ok: bool
    why: str = ""
    problem: Problem | None = None
    plan: Plan | None = None
    decisions: list[Decision] = field(default_factory=list)
    covered: frozenset[str] = frozenset()
    """The levers it decided for; the rules decide the rest."""
    notes: list[tuple[str, str]] = field(default_factory=list)
    """Goals it left to the rules, by scope, and why: a message for the web UI's catalog."""
    watched: Watched = field(default_factory=Watched)


class Optimizer:
    def __init__(
        self,
        db: Database,
        values: Values,
        host: PluginHost,
        learnings: Learnings,
        weather: "Weather | None" = None,
    ) -> None:
        self._db = db
        self._values = values
        self._host = host
        self._learnings = learnings
        self._weather = weather
        self.last: Optimized | None = None
        self._stopped: float | None = None
        self._was_on: bool | None = None
        self._expected: dict[str, dict[float, float]] = {}
        """Per climate system: how warm each plan expected its rooms to be an hour on, by
        when."""
        self.drift: dict[str, float] = {}
        """Per climate system: the learned correction of the house model; the planner's
        memory, kept across a restart."""
        self._watched = Watched()

    # --- a round ---------------------------------------------------------------------------

    async def run(self, sit: Situation, resolver: Resolver) -> Optimized:
        try:
            problem, covered, notes = await self.problem(sit, resolver)
        except Exception as e:
            log.exception("the optimizer's problem couldn't be made")
            self.last = Optimized(False, f"{e}")
            return self.last
        watched = self._watched
        if not covered:
            self.last = Optimized(False, NOTHING, problem, notes=notes, watched=watched)
            return self.last
        found = await asyncio.to_thread(solve, problem, TIME_LIMIT_S)
        if found.ok:
            for scope, planned in found.systems.items():
                if planned.room:
                    kept = self._expected.setdefault(scope, {})
                    kept[problem.slots[0].t + 3600] = planned.room[0]
                    for t in [t for t in kept if t < problem.slots[0].t]:
                        del kept[t]
        if not found.ok:
            self.last = Optimized(False, found.why, problem, found, notes=notes, watched=watched)
            return self.last
        decisions = levers.decide(found, problem, await self.now(sit, problem))
        mine = [replace_planner(d) for d in decisions if d.lever in covered]
        self.last = Optimized(True, "", problem, found, mine, covered, notes, watched)
        return self.last

    async def preview(self, sit: Situation, resolver: Resolver) -> Optimized:
        """The plan as it would be now, kept nowhere: for an answer as something is asked.
        What the plans an hour ago expected isn't checked off by it, nor the drift moved."""
        try:
            problem, covered, notes = await self.problem(sit, resolver, preview=True)
        except Exception as e:
            log.exception("the optimizer's problem couldn't be made")
            return Optimized(False, f"{e}")
        watched = self._watched
        if not covered:
            return Optimized(False, NOTHING, problem, notes=notes, watched=watched)
        found = await asyncio.to_thread(solve, problem, TIME_LIMIT_S)
        return Optimized(found.ok, found.why, problem, found, notes=notes, watched=watched)

    async def between(self, sit: Situation) -> list[Decision]:
        """Between slots, only hot water: from the slot's plan, with what is seen now."""
        last = self.last
        if last is None or not last.ok or last.plan is None or last.problem is None:
            return []
        tanks_only = Problem(
            last.problem.slots,
            tanks=last.problem.tanks,
            ranking=last.problem.ranking,
            slider=last.problem.slider,
        )
        decisions = levers.decide(last.plan, tanks_only, await self.now(sit, tanks_only))
        return [replace_planner(d) for d in decisions if d.lever in last.covered]

    # --- the problem -----------------------------------------------------------------------

    async def problem(
        self, sit: Situation, resolver: Resolver, *, preview: bool = False
    ) -> tuple[Problem, frozenset[str], list[tuple[str, str]]]:
        t0 = sit.now.timestamp() // 900 * 900
        starts = [t0 + 900 * j for j in range(SLOTS)]
        self._watched = Watched()
        covered: set[str] = set()
        notes: list[tuple[str, str]] = []
        slots = await self._slots(sit, starts)
        units = self._units()
        systems = []
        for system in sorted(sit.caps.systems):
            found, why = await self._system(sit, resolver, system, units, starts, preview)
            if found is None:
                notes.append((system, why))
                continue
            systems.append(found)
            covered.add(f"{system}/heating.offset")
        tanks = []
        for scope in sorted(sit.tanks):
            found_tank, why = await self._tank(sit, scope, units, starts)
            if found_tank is None:
                notes.append((scope, why))
                continue
            tanks.append(found_tank)
            covered |= {f"{scope}/block", f"{scope}/boost_once"}
        pools = []
        for scope in sorted(sit.pools):
            found_pool, why = self._pool(sit, scope, units)
            if found_pool is None:
                notes.append((scope, why))
                continue
            pools.append(found_pool)
            covered |= {f"{scope}/start_temp", f"{scope}/stop_temp", f"{scope}/block"}
        addition = self._addition(sit)
        if addition is not None and systems:
            covered |= {
                ref
                for ref in sit.levers
                if ref.endswith(("/addition/stop_temp", "/addition/max_power"))
            }
        else:
            addition = None
        published = [p for p in sit.prices if not p.estimated]
        slider = sit.force.slider if published else None
        problem = Problem(
            tuple(slots),
            tuple(systems),
            tuple(tanks),
            tuple(pools),
            addition,
            tuple(sit.force.ranking),
            slider,
        )
        return problem, frozenset(c for c in covered if c in sit.levers), notes

    def _units(self) -> dict[str, dict[str, Any]]:
        """Per pump unit, `instance:path`: its compressor's documented figures, its tanks'
        volumes, and its brine point."""
        out: dict[str, dict[str, Any]] = {}
        for instance_id, instance in sorted(self._host.instances.items()):
            described = instance.described
            if described is None:
                continue
            paths = {p.path for p in described.points}
            for unit in (n.path for n in described.nodes if n.kind == "unit"):
                below = [n for n in described.nodes if n.path.startswith(f"{unit}/")]
                compressor = next((n for n in below if n.kind == "compressor"), None)
                brine = next(
                    (
                        Key(instance_id, p)
                        for p in sorted(paths)
                        if p.startswith(f"{unit}/") and p.endswith("/brine.in.temp")
                    ),
                    None,
                )
                out[f"{instance_id}:{unit}"] = {
                    "rating": compressor.rating if compressor is not None else None,
                    "liters": {
                        f"{instance_id}:{n.path}": n.rating.liters
                        for n in below
                        if n.kind == "dhw_tank" and n.rating is not None and n.rating.liters
                    },
                    "brine": brine,
                    "compressor": next(
                        (
                            Key(instance_id, f"{n.path}/state")
                            for n in below
                            if n.kind == "compressor" and f"{n.path}/state" in paths
                        ),
                        None,
                    ),
                }
        return out

    @staticmethod
    def _unit_of(scope: str) -> str:
        instance, path = split(scope)
        return f"{instance}:{path.split('/')[0]}"

    async def _slots(self, sit: Situation, starts: list[float]) -> list[Slot]:
        prices = {p.start.timestamp(): p for p in sit.prices}
        weather = await self._forecasts(sit, starts)
        brine = await self._brine(sit)
        limit = rules.power_limit(sit)
        other = await self._other_kw(sit)
        published = [p for p in sit.prices if not p.estimated]
        out = []
        for j, t in enumerate(starts):
            price = prices.get(t)
            at = datetime.fromtimestamp(t, UTC)
            signal = rules.signal(published, at) if price is not None else None
            outdoor, sun, wind = weather[j]
            out.append(
                Slot(
                    t,
                    price=price.value if price is not None else None,
                    estimated=price.estimated if price is not None else False,
                    outdoor=outdoor,
                    sun=sun,
                    wind=wind,
                    source=brine if brine is not None else outdoor,
                    power_limit=limit,
                    other_kw=other,
                    expensive=signal is not None and signal < rules.EXPENSIVE,
                )
            )
        return out

    async def _forecasts(
        self, sit: Situation, starts: list[float]
    ) -> list[tuple[float, float, float]]:
        """Per slot: the outdoor temperature (its learned bias taken off), the sun, the
        wind. Where nothing is forecast, the outdoor temperature as it is now."""
        from ..core.sensors import SITE
        from ..core.weather import INSTANT, value_at

        now_out = self._number(self._values.latest.get(Key(SITE, "outdoor/temperature")))
        if now_out is None:
            now_out = next(
                (
                    self._number(e)
                    for k, e in sorted(self._values.latest.items(), key=lambda kv: kv[0].instance)
                    if k.point.endswith("/outdoor.temp") and self._number(e) is not None
                ),
                None,
            )
        fallback = now_out if now_out is not None else 0.0
        first = datetime.fromtimestamp(starts[0], UTC)
        last = datetime.fromtimestamp(starts[-1] + 900, UTC)
        found: dict[str, Any] = {}
        if self._weather is not None:
            for quantity in ("temperature", "irradiance.global", "wind_speed"):
                try:
                    found[quantity] = await self._weather.forecast(quantity, first, last)
                except Exception:
                    log.exception("the %s forecast couldn't be read for the optimizer", quantity)
        out = []
        for t in starts:
            mid = datetime.fromtimestamp(t + 450, UTC)
            hours = (t + 450 - sit.now.timestamp()) / 3600
            temp = None
            if "temperature" in found:
                temp = value_at(found["temperature"].spans, mid, True)
                if temp is not None:
                    temp -= correction(self._learnings, found["temperature"].source, max(0, hours))
            sun = (
                value_at(found["irradiance.global"].spans, mid, "irradiance.global" in INSTANT)
                if "irradiance.global" in found
                else None
            )
            wind = value_at(found["wind_speed"].spans, mid, True) if "wind_speed" in found else None
            out.append((temp if temp is not None else fallback, sun or 0.0, wind or 0.0))
        return out

    async def _brine(self, sit: Situation) -> float | None:
        """The brine as it came in over the last hour."""
        for unit in self._units().values():
            if unit["brine"] is not None:
                found = await self._hour_mean(unit["brine"], sit.now.timestamp())
                if found is not None:
                    return found
        return None

    async def _other_kw(self, sit: Situation) -> float:
        """The household's own load over the last hour: the house's, less the pump's (the
        compressor's learned draw for the share of the hour it ran, and the addition's
        power). Without the house's power, none."""
        found = GridLimits(self._db, self._values).house_power()
        if found is None:
            return 0.0
        now = sit.now.timestamp()
        house = await self._hour_mean(found[0], now)
        if house is None:
            return 0.0
        pump = 0.0
        for unit, info in self._units().items():
            if info["compressor"] is not None:
                ran = await self._hour_mean(info["compressor"], now, _running)
                draw = self._draw(unit, "heating", info.get("rating"))
                pump += (ran or 0.0) * (draw or 0.0)
            instance, path = split(unit)
            added = await self._hour_mean(Key(instance, f"{path}/addition/power"), now)
            pump += added or 0.0
        return max(0.0, house * found[1] - pump)

    def _draw(self, unit: str, purpose: str, rating: Rating | None) -> float | None:
        draws = self._learnings.used(DRAW, unit)
        if isinstance(draws, dict):
            for p in (purpose, "heating"):
                if isinstance(draws.get(p), int | float):
                    return float(draws[p])
        if rating is not None and rating.heating:
            return rating.heating[0].elec_kw
        return None

    async def _system(
        self,
        sit: Situation,
        resolver: Resolver,
        system: str,
        units: dict[str, dict[str, Any]],
        starts: list[float],
        preview: bool = False,
    ) -> tuple[System | None, str]:
        lever = sit.levers.get(f"{system}/heating.offset")
        if lever is None:
            return None, NO_OFFSET
        if not sit.heating:
            return None, HEATING_OFF
        model = self._learnings.used(MODEL, system)
        water = self._learnings.used(WATER, system)
        k = self._learnings.used(EMITTERS, system)
        stored = self._learnings.used(STORED, system)
        if not isinstance(model, dict):
            return None, HOUSE_UNPROVED
        if not isinstance(water, dict):
            return None, WATER_UNPROVED
        if not isinstance(k, int | float) or k <= 0:
            return None, EMITTERS_UNPROVED
        unit = self._unit_of(system)
        info = units.get(unit, {})
        share = self._learnings.used(HEATING, unit)
        draw = self._draw(unit, "heating", info.get("rating"))
        if not isinstance(share, int | float) or draw is None:
            return None, NO_COP
        rooms = [
            r
            for r in sit.rooms
            if r.system == system and r.temp is not None and r.weight > 0 and not r.airing
        ]
        if not rooms:
            return None, NO_ROOM
        # The house was learned on hourly means: it starts from the rooms' last hour too,
        # not from the air as it swings with each run of the compressor.
        temps = {
            r.scope: await self._room_mean(r.scope, sit.now.timestamp(), r.temp) for r in rooms
        }
        mean = statistics.mean(temps.values())
        if preview:
            drift = self.drift.get(system, 0.0)
        else:
            drift = self._came_out(system, sit.now.timestamp(), mean)
        fit = Fit(
            float(model["alpha"]),
            float(model["beta"]),
            float(model["sigma"]),
            float(model["omega"]),
            float(model["gamma"]),
            float(model["r"]),
            int(model["lag"]),
            0,
            float(model.get("error", 0.0)),
        )
        curve = Water.from_value(water)
        # Not beyond what the house was learned on, a step past it at most: lowered further,
        # the pump stops heating altogether, which the learned curve doesn't know either.
        lowest = max(lever.low if lever.low is not None else -10.0, curve.low - BEYOND)
        highest = min(lever.high if lever.high is not None else 10.0, curve.high + BEYOND)
        margin = min(MARGIN_MOST, max(MARGIN_LEAST, MARGIN_ERRORS * fit.error))
        bands: dict[str, list[Band]] = {r.scope: [] for r in rooms}
        lows: dict[str, list[float | None]] = {r.scope: [] for r in rooms}
        whose: dict[str, list[tuple[str, ...]]] = {r.scope: [] for r in rooms}
        for h in range(HORIZON_H):
            at = datetime.fromtimestamp(starts[0] + 3600 * h + 1800, UTC)
            force = resolver.in_force(at, heating=sit.heating)
            for r in rooms:
                bound = force.bound("room_temp", r.scope) or force.bound("room_temp", system)
                bands[r.scope].append(_inside(bound, margin))
                lows[r.scope].append(bound.low if bound is not None else None)
                whose[r.scope].append(bound.by if bound is not None else ())
        reach = rules._response_h(sit, system)
        for r in rooms:
            self._watched.rooms[r.scope] = RoomGoal(
                system, r.name, tuple(lows[r.scope]), tuple(whose[r.scope]), reach
            )
        current = rules._number(lever.current, rules._number(lever.baseline))
        before = await self._water_before(system, sit.now.timestamp(), fit.lag)
        return (
            System(
                system,
                fit,
                curve,
                float(k),
                min(lowest, current),
                max(highest, current),
                current,
                mean,
                before,
                tuple(
                    Room(r.scope, temps[r.scope] - mean, tuple(bands[r.scope]), r.name)
                    for r in rooms
                ),
                float(share),
                draw,
                float(stored) if isinstance(stored, int | float) else 0.0,
                drift,
            ),
            "",
        )

    async def _room_mean(self, scope: str, now: float, fallback: float | None) -> float:
        """A room's mean temperature over the last hour."""
        from ..core.sensors import SITE

        key = Key(SITE, f"room.{scope.removeprefix('room:')}/temperature")
        found = await self._hour_mean(key, now)
        if found is not None:
            return found
        return fallback if fallback is not None else 20.0

    async def _hour_mean(
        self, key: Key, now: float, of: Callable[[Sample], float | None] = _numeric
    ) -> float | None:
        """A point's mean over the last hour; None where it has none. Read only up to what
        is surely written: whether this minute's reading came in before the round must not
        change the plan, or shadow wouldn't decide as control does."""
        now = settled(now)
        return await self._mean(key, now - 3600, now, of)

    async def _last(self, key: Key, now: float) -> float | None:
        """A point's value as it stood at the last minute surely written; where it hasn't
        moved for hours, as it is."""
        now = settled(now)
        samples = await self._values.history(key, now - 2 * 3600, now)
        found = [s for s in samples if s.value is not None]
        if found:
            return found[-1].value if found[-1].quality == "good" else None
        return self._number(self._values.latest.get(key))

    async def _mean(
        self, key: Key, start: float, end: float, of: Callable[[Sample], float | None] = _numeric
    ) -> float | None:
        """A point's mean from `start` to `end` (`held_mean`)."""
        return held_mean(await self._values.history(key, start - 3600, end), start, end, of)

    def _came_out(self, system: str, now: float, room: float) -> float:
        """How the rooms came out against what the plan an hour ago expected for now: a
        share of each miss is carried as a drift the house model didn't foresee, so a
        model that is a little off is corrected by what it sees."""
        expected = self._expected.get(system, {}).pop(now // 900 * 900, None)
        drift = self.drift.get(system, 0.0)
        if expected is not None:
            drift += DRIFT_GAIN * (room - expected)
            drift = max(-DRIFT_MOST, min(DRIFT_MOST, drift))
            self.drift[system] = drift
        return drift

    async def _water_before(self, system: str, now: float, lag: int) -> tuple[float, ...]:
        """The system's water, the mean of its supply and return, in each of the last hours."""
        instance, path = split(system)
        now = settled(now)
        hours = max(lag, 1)
        out = []
        for h in range(hours, 0, -1):
            t0, t1 = now - 3600 * h, now - 3600 * (h - 1)
            means = []
            for name in ("supply.temp", "return.temp"):
                found = await self._mean(Key(instance, f"{path}/{name}"), t0, t1)
                if found is not None:
                    means.append(found)
            if means:
                out.append(sum(means) / len(means))
        return tuple(out)

    async def _tank(
        self, sit: Situation, scope: str, units: dict[str, dict[str, Any]], starts: list[float]
    ) -> tuple[Tank | None, str]:
        tank = sit.tanks[scope]
        block = sit.levers.get(f"{scope}/block")
        boost = sit.levers.get(f"{scope}/boost_once")
        if block is None and boost is None:
            return None, NO_TANK_LEVER
        if tank.loss_per_h is None or tank.top is None:
            return None, TANK_UNPROVED
        unit = self._unit_of(scope)
        info = units.get(unit, {})
        rated = await self._db.get(Rated) or Rated()
        liters = info.get("liters", {}).get(scope) or rated.liters.get(scope)
        if not liters:
            return None, NO_VOLUME
        share = self._learnings.used(DHW, unit)
        draw = self._draw(unit, "dhw", info.get("rating"))
        if not isinstance(share, int | float) or draw is None:
            return None, NO_COP
        instance, path = split(scope)
        now = sit.now.timestamp()
        found = await self._last(Key(instance, f"{path}/temp.top"), now)
        top = found if found is not None else tank.top
        low = await self._last(Key(instance, f"{path}/temp.charge"), now)
        level = (top + low) / 2 if low is not None else top
        hour0 = sit.now.timestamp() // 3600 * 3600
        draws = []
        for t in starts:
            i = int((t - hour0) // 3600)
            draws.append(tank.draws[i] / 4 if 0 <= i < len(tank.draws) else 0.0)
        ceiling = self._learnings.used(CEILING, scope)
        top = float(ceiling) if isinstance(ceiling, int | float) else (tank.stop or 55.0)
        floor = sit.force.bound("tank_top_temp", scope)
        deadlines = []
        for d in sit.deadlines:
            if d.scope not in (scope, "house"):
                continue
            slot = math.ceil((d.t.timestamp() - starts[0]) / 900) - 1
            if 0 <= slot < len(starts):
                margin = MUST_MARGIN if d.strength == "must" else 0.0
                deadlines.append(Deadline(slot, d.at_least + margin, d.rank, d.by))
                goal = DeadlineGoal(d.by, scope, slot, d.at_least, d.strength)
                self._watched.deadlines.append(goal)
        return (
            Tank(
                scope,
                level,
                liters * WATER_KJ_L_K / 3600,
                tank.loss_per_h,
                tuple(draws),
                max(top, level),
                float(share),
                draw,
                floor=floor.low if floor is not None else None,
                least=tank.start if block is None else None,
                deadlines=tuple(deadlines),
                room_c=rules.TANK_ROOM_C,
            ),
            "",
        )

    def _pool(
        self, sit: Situation, scope: str, units: dict[str, dict[str, Any]]
    ) -> tuple[Pool | None, str]:
        temp = sit.pools.get(scope)
        model = self._learnings.used(POOL, scope)
        bound = sit.force.bound("pool_temp", scope)
        if not any(f"{scope}/{n}" in sit.levers for n in ("start_temp", "stop_temp", "block")):
            return None, NO_POOL_LEVER
        if not isinstance(model, dict) or temp is None:
            return None, POOL_UNPROVED
        if bound is None or bound.low is None or bound.high is None:
            return None, NO_BAND
        unit = self._unit_of(scope)
        draw = self._draw(unit, "pool", units.get(unit, {}).get("rating"))
        if draw is None:
            return None, NO_DRAW
        return (
            Pool(
                scope,
                temp,
                float(model["a"]),
                float(model["b"]),
                (Band(bound.low, bound.high),) * SLOTS,
                draw,
            ),
            "",
        )

    def _addition(self, sit: Situation) -> Addition | None:
        stop = next((lv for r, lv in sit.levers.items() if r.endswith("/addition/stop_temp")), None)
        power = next(
            (lv for r, lv in sit.levers.items() if r.endswith("/addition/max_power")), None
        )
        if stop is None and power is None:
            return self._own_addition()
        bound = sit.force.bound("addition", "house")
        policy = str(bound.value) if bound is not None and bound.value is not None else "pump"
        kw = rules._number(power.current, power.high or ADDITION_KW) if power else ADDITION_KW
        limit = bound.high if bound is not None else None
        return Addition(kw, policy, limit)

    def _own_addition(self) -> Addition | None:
        """With neither of the addition's levers in use, the pump's own addition: its power
        and its stop as the pump has them, read back through those levers, and the mean
        outdoor temperature its stop is set against. None where the pump doesn't say."""
        for instance_id, instance in sorted(self._host.instances.items()):
            described = instance.described
            if described is None:
                continue
            found: dict[str, float] = {}
            unit = None
            for lever in described.levers:
                point = lever.verify.point
                if lever.path.endswith(("/addition/max_power", "/addition/stop_temp")) and point:
                    unit = lever.path.partition("/addition/")[0]
                    value = self._number(self._values.latest.get(Key(instance_id, point)))
                    if value is not None:
                        found[lever.path.rpartition("/")[2]] = value
            power = found.get("max_power")
            if unit is None or power is None or power <= 0:
                continue
            mean = self._number(
                self._values.latest.get(Key(instance_id, f"{unit}/outdoor.temp.mean"))
            )
            return Addition(power, own=True, stop_c=found.get("stop_temp"), mean_c=mean)
        return None

    # --- what is seen now ------------------------------------------------------------------

    async def now(self, sit: Situation, problem: Problem) -> "levers.Now":
        tanks = {}
        for scope, tank in sit.tanks.items():
            floor = sit.force.bound("tank_top_temp", scope)
            tanks[scope] = levers.TankNow(
                tank.top, floor.low if floor is not None else None, sit.demand == "dhw"
            )
        below: dict[str, str] = {}
        above: dict[str, str] = {}
        for room in sit.rooms:
            band = rules._band(sit, room)
            if room.temp is None or band is None or room.weight <= 0:
                continue
            name = room.name or rules._name(room.scope)
            if room.temp < band[0]:
                below.setdefault(room.system, name)
            elif room.temp > band[1]:
                above.setdefault(room.system, name)
        on, stopped = self._compressor(sit)
        starts = await self._starts_today(sit)
        pace = None
        for unit in self._units():
            found = self._learnings.used(STARTS, unit)
            if isinstance(found, int | float):
                pace = float(found)
        bound = sit.force.bound("addition", "house")
        local = sit.now.astimezone(sit.zone)
        midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
        recent = await self._recent(midnight.timestamp())
        return levers.Now(
            sit.now,
            sit.zone,
            sit.levers,
            tanks=tanks,
            pools=dict(sit.pools),
            rooms_below=below,
            rooms_above=above,
            shifting=sit.force.slider is not None and any(not p.estimated for p in sit.prices),
            budget=sit.budget,
            compressor_on=on,
            stopped_s=stopped,
            starts_today=starts,
            pace=pace,
            policy=str(bound.value) if bound is not None and bound.value is not None else None,
            ranks={
                "comfort_low": sit.force.rank("comfort_low"),
                "comfort_high": sit.force.rank("comfort_high"),
            },
            recent=recent,
        )

    async def _recent(self, since: float) -> dict[str, int]:
        """Per lever, the changes sent (or in shadow, that would have been) since `since`."""
        counted = sorted(COUNTED)

        def read(t: Transaction) -> dict[str, int]:
            marks = ", ".join("?" * len(counted))
            rows = t.execute(
                f"SELECT lever, COUNT(*) FROM acts WHERE t >= ? AND outcome IN ({marks})"  # noqa: S608 - marks, not input
                " GROUP BY lever",
                (since, *counted),
            )
            return {lever: n for lever, n in rows}

        return await self._db.run(read)

    def _compressor(self, sit: Situation) -> tuple[bool, float | None]:
        """Whether the compressor runs now, and if it stands, how long since it stopped."""
        on = False
        for info in self._units().values():
            if info["compressor"] is None:
                continue
            state = self._values.latest.get(info["compressor"])
            on = on or (state is not None and state.quality == "good" and state.value == "running")
        now = sit.now.timestamp()
        if self._was_on and not on:
            self._stopped = now
        self._was_on = on
        return on, None if on or self._stopped is None else now - self._stopped

    async def _starts_today(self, sit: Situation) -> int | None:
        local = sit.now.astimezone(sit.zone)
        midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
        since = midnight.timestamp()
        count: int | None = None
        for info in self._units().values():
            if info["compressor"] is None:
                continue
            until = settled(sit.now.timestamp())
            samples = await self._values.history(info["compressor"], since, until)
            was = None
            n = 0
            for s in samples:
                running = s.quality == "good" and s.text == "running"
                if running and was is False:
                    n += 1
                was = running
            count = (count or 0) + n
        return count

    @staticmethod
    def _number(envelope: Any) -> float | None:
        if envelope is None or envelope.quality != "good":
            return None
        value = envelope.value
        if isinstance(value, int | float) and not isinstance(value, bool):
            return float(value)
        return None


def settled(now: float) -> float:
    """The end of the history that is surely written: values are written every minute, and
    the last minute's may not be yet."""
    return now // 60 * 60 - 60


def replace_planner(d: Decision) -> Decision:
    return replace(d, planner="optimizer")


def _inside(bound: Any, margin: float) -> Band:
    """A room's band less a margin on each side, for the house model's own error; a band
    narrower than both margins is planned at its middle."""
    if bound is None:
        return Band()
    low, high = bound.low, bound.high
    if low is not None and high is not None and high - low < 2 * margin:
        middle = (low + high) / 2
        return Band(middle, middle)
    return Band(None if low is None else low + margin, None if high is None else high - margin)
