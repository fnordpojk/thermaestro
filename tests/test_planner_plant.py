"""The planner against the plant, over simulated days: the behavior contracts that need a
planner. Comfort and the hot-water floor hold, extreme prices buy neither cold nor heat,
shifting nets out, the write budget holds, the heating stop is followed, a lost room sensor
puts the offset back, a value not kept isn't asked for again, and shadow decides as
control would."""

import asyncio
import itertools
import json
import math
import warnings
from collections.abc import Awaitable, Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from functools import partial
from pathlib import Path

import pytest
from plant import Plant, Scenario, draw
from plant.harness import PLANT, Frame, Sim, running
from plant.market import day_ahead, known
from plant.optimizing import PlantWeather, teach
from plant.planning import CS, TANK, intended, planning
from plant.scenario import Emitter
from plant.vloop import simulate

from thermaestro import clock
from thermaestro.core import AuditLog
from thermaestro.core.events import Event, Events
from thermaestro.intents import RANKS, Intent, Intents, Level, kinds
from thermaestro.learn import Learnings
from thermaestro.store import Control, Database, LeverMode, Plugin

LEVERS: dict[str, LeverMode] = {
    "cs1/heating.offset": "control",
    "dhw/block": "control",
    "dhw/boost_once": "control",
}
LEVELS = [Level(id="day", name="Day", scope=CS, low=20.5, high=22.0)]
FLOOR = 42.0
DAY_S = 86_400.0
OFFSET = "pump:hp1/cs1/heating.offset"

Intended = list[tuple[float, str, str, str]]
BOTH = [
    pytest.param(False, id="rules"),
    pytest.param(True, id="optimizer", marks=pytest.mark.nightly),
]
"""A contract with either planner driving: the optimizer's runs nightly, for time."""
PLANNED: dict[Path, list[tuple[str, int]]] = {}
"""Per run, by its path: which planner made each decision kept, and whether it drove."""
OUTCOMES: dict[Path, list[str]] = {}
"""Per run's sim path: each slot's optimizer outcome, and what it left to the rules."""


def run[T](s: Scenario, body: Callable[[], Awaitable[T]]) -> T:
    async def main() -> T:
        return await body()

    return simulate(main, s.start_time)


def household(*, slider: float | None = 0.5, rooms: bool = True) -> list[Intent]:
    made = {"principal": "user:test", "created": clock.now() - timedelta(minutes=1)}
    out = [kinds.hot_water_floor(TANK, FLOOR, **made)]  # type: ignore[arg-type]
    if rooms:
        out.append(kinds.comfort_band(CS, [("day", ((), None, None))], **made))  # type: ignore[arg-type]
    else:
        out.append(kinds.no_sensor_band(CS, steps=2, **made))  # type: ignore[arg-type]
    if slider is not None:
        out.append(kinds.cost_stance(ranking=RANKS, slider=slider, **made))  # type: ignore[arg-type]
    return out


def simulated(
    path: Path,
    s: Scenario,
    days: float,
    *,
    slider: float | None = 0.5,
    rooms: bool = True,
    levers: dict[str, LeverMode] | None = None,
    spike: float = 1.0,
    negative: bool = False,
    record: bool = False,
    replay: list[Frame] | None = None,
    during: Callable[[Sim], Awaitable[None]] | None = None,
    optimizer: bool = False,
    hindsight: bool = False,
    more: Callable[[], list[Intent]] | None = None,
) -> tuple[Sim, Intended]:
    """Run the planner on the plant for `days`; the sim, and the planner's intended writes.
    As in a house: each day's draws differ, and the prices are those published by then,
    estimated beyond. `optimizer`: what the learners would have made of the house is known,
    the forecast is off as a provider's is, and the optimizer drives. `hindsight`: the
    optimizer, told the truth: the weather as it comes, every price, each day's draws.
    `more`: intents of the household's besides, made as the run starts."""

    path.mkdir(parents=True, exist_ok=True)
    optimizer = optimizer or hindsight
    s = replace(s, varied=True)

    async def body() -> tuple[Sim, Intended]:
        sim = Sim(s, path, levers or LEVERS, record=record, replay=replay)
        async with running(sim):
            series = day_ahead(
                s.seed, s.start_time, days, spike=spike, negative=negative, history_days=28
            )
            # Told the truth: every price from the day before, none of the weeks kept
            # for estimates.
            since = s.start_time - timedelta(days=1)
            truth = [p for p in series if p.start >= since]
            prices = truth if hindsight else partial(known, series)
            intents = household(slider=slider, rooms=rooms) + (more() if more else [])
            learnings, weather = None, None
            if optimizer:
                learnings = Learnings(sim.db, AuditLog(path / "learn-audit"))
                await learnings.load()
                if hindsight:
                    assert days <= 7, "a week of draws holds a day of each weekday"
                    when = [
                        (s.start_time + timedelta(days=i)).date() for i in range(math.ceil(days))
                    ]
                    await teach(learnings, s, when)
                else:
                    await teach(learnings, s)
                control = await sim.db.get(Control) or Control()
                await sim.db.put(control.model_copy(update={"planner": "optimizer"}))
                weather = PlantWeather(s, truth=hindsight)
            async with planning(
                sim, intents, LEVELS, prices, rooms=rooms, learnings=learnings, weather=weather
            ):
                if during is not None:
                    await during(sim)
                await sim.run(days - (clock.time() - s.start_time.timestamp()) / DAY_S)
            PLANNED[path] = await driven(sim)
            return sim, await intended(sim)

    return run(s, body)


def traced(sim: Sim, start: datetime, after_days: float, name: str) -> list[float]:
    since = start.timestamp() + after_days * DAY_S
    return [row[name] for row in sim.trace if row["t"] >= since]


def indoor(sim: Sim, start: datetime, after_days: float) -> list[float]:
    """The room's hourly mean, slot by slot: the air swings with every run of the
    compressor, the room as a person feels it doesn't."""
    air = [row[0] for row in traced(sim, start, after_days - 1 / 24, "indoor")]  # type: ignore[index]
    return [sum(air[i - 3 : i + 1]) / 4 for i in range(3, len(air))]


def floor_holds(sim: Sim, start: datetime) -> bool:
    """The tank's top below its floor (and the measuring's degree) only for as long as a
    draw takes to recover from: at most two slots in a row."""
    tops = traced(sim, start, 0.0, "top")
    run = 0
    for top in tops:
        run = run + 1 if top < FLOOR - 1.0 else 0
        if run > 2:
            return False
    return True


def per_day(writes: Intended, start: datetime) -> list[int]:
    days: dict[int, int] = {}
    for slot, _, _, _ in writes:
        day = int((slot - start.timestamp()) // DAY_S)
        days[day] = days.get(day, 0) + 1
    return list(days.values())


def offsets(writes: Intended) -> list[tuple[float, float]]:
    return [
        (slot, json.loads(params)["value"]) for slot, lever, _, params in writes if lever == OFFSET
    ]


# --- the contracts ------------------------------------------------------------------------


def test_comfort_the_floor_and_the_budget_hold(tmp_path: Path) -> None:
    s = draw(3, emitter="radiators")
    sim, writes = simulated(tmp_path, s, 2.0)
    temps = indoor(sim, s.start_time, 1.0)
    inside = sum(20.0 <= t <= 22.5 for t in temps) / len(temps)
    assert inside >= 0.9, (min(temps), max(temps))
    assert floor_holds(sim, s.start_time)
    assert offsets(writes)  # it steered
    assert max(per_day(writes, s.start_time)) <= 50  # the soft budget


@pytest.mark.parametrize("optimizer", BOTH)
def test_extreme_prices_buy_neither_cold_nor_heat(tmp_path: Path, optimizer: bool) -> None:
    s = draw(4, emitter="radiators")
    sim, _ = simulated(tmp_path, s, 2.0, slider=1.0, spike=25.0, negative=True, optimizer=optimizer)
    temps = indoor(sim, s.start_time, 1.0)
    assert min(temps) >= 19.5
    assert max(temps) <= 23.0
    assert floor_holds(sim, s.start_time)


def test_without_a_room_sensor_the_shift_nets_out(tmp_path: Path) -> None:
    s = draw(5, emitter="radiators")
    sim, writes = simulated(tmp_path, s, 2.0, rooms=False, slider=1.0)
    changes = offsets(writes)
    base = sim.executor.claims[OFFSET].baseline
    assert isinstance(base, int | float)
    assert {value - base for _, value in changes} >= {1.0, -1.0}  # raised and cut
    # Each value held for its time, the second day's offset averages the pump's own.
    start, end = s.start_time.timestamp() + DAY_S, s.start_time.timestamp() + 2 * DAY_S
    level = float(base)
    for slot, value in changes:
        if slot <= start:
            level = value
    total, since = 0.0, start
    for slot, value in changes:
        if start < slot < end:
            total += level * (slot - since)
            level, since = value, slot
    total += level * (end - since)
    assert abs(total / DAY_S - base) <= 0.5


@pytest.mark.parametrize("optimizer", BOTH)
def test_the_heating_stop_leaves_heating_to_the_pump(tmp_path: Path, optimizer: bool) -> None:
    s = draw(6, emitter="radiators", mean_c=25.0)
    _, writes = simulated(tmp_path, s, 1.5, optimizer=optimizer)
    assert offsets(writes) == []


@pytest.mark.parametrize("optimizer", BOTH)
def test_a_lost_room_sensor_puts_the_offset_back(tmp_path: Path, optimizer: bool) -> None:
    s = draw(7, emitter="radiators")
    stopped: list[float] = []

    async def silence(sim: Sim) -> None:
        await sim.run(1.0)
        await sim.db.put(Plugin(plugin="plant_sensors", enabled=False), PLANT)
        await sim.host.apply(PLANT)
        stopped.append(clock.time())

    sim, writes = simulated(
        tmp_path, s, 1.5, slider=1.0, spike=3.0, during=silence, optimizer=optimizer
    )
    claim = sim.executor.claims[OFFSET]
    assert not claim.changed  # back as found
    later = [w for w in writes if w[0] > stopped[0] + 2 * 3600 and w[1] == OFFSET]
    assert later == []  # and left alone


def test_a_value_not_kept_is_not_asked_for_again_at_once(tmp_path: Path) -> None:
    s = draw(8, emitter="radiators")

    async def refuse(sim: Sim) -> None:
        assert isinstance(sim.plant, Plant)
        sim.plant.registers[47011] = -5 & 0xFF  # a cold house, so the planner asks
        sim.plant.not_kept.add(47011)

    _, writes = simulated(tmp_path, s, 1.0, slider=1.0, during=refuse)
    asked = [w for w in writes if w[1] == OFFSET]
    assert asked  # it tried
    by_value: dict[str, list[float]] = {}
    for slot, _, _, params in asked:
        by_value.setdefault(params, []).append(slot)
    for slots in by_value.values():
        gaps = [b - a for a, b in itertools.pairwise(slots)]
        assert all(gap >= 6 * 3600 for gap in gaps), gaps


# --- shadow decides as control would ------------------------------------------------------


def shadow_equals_control(path: Path, s: Scenario, days: float, *, optimizer: bool = False) -> None:
    controls: dict[str, LeverMode] = dict.fromkeys(LEVERS, "control")
    shadows: dict[str, LeverMode] = dict.fromkeys(LEVERS, "shadow")
    done, wanted = simulated(
        path / "control", s, days, levers=controls, record=True, optimizer=optimizer
    )
    copied, shadowed = simulated(
        path / "shadow",
        s,
        days,
        levers=shadows,
        replay=done.frames,
        optimizer=optimizer,
    )
    # A round at the very moment a run ends may or may not finish before it is stopped:
    # the optimizer's solve runs in a thread. Compared up to then.
    end = s.start_time.timestamp() + days * DAY_S
    wanted = [w for w in wanted if w[0] < end]
    shadowed = [w for w in shadowed if w[0] < end]
    assert len(wanted) >= 4 * days
    assert shadowed == wanted
    assert copied.bus.writes == []


def test_shadow_decides_as_the_planner_in_control(tmp_path: Path) -> None:
    shadow_equals_control(tmp_path, draw(1, emitter="radiators"), days=1.0)


# --- with the optimizer driving -------------------------------------------------------------


async def driven(sim: Sim) -> list[tuple[str, int]]:
    rows = await sim.db.run(lambda t: t.execute("SELECT planner, driving FROM planned").fetchall())
    plans = await sim.db.run(lambda t: t.execute("SELECT body FROM plans").fetchall())
    for (body,) in plans:
        found = json.loads(body)
        why = found.get("why") or ""
        notes = "; ".join(f"{scope}: {note}" for scope, note in found.get("notes", []))
        OUTCOMES.setdefault(sim.path, []).append(f"{found.get('ok')} {why} {notes}".strip())
    return [(planner, driving) for planner, driving in rows]


def test_the_optimizer_keeps_comfort_the_floor_and_the_budget(tmp_path: Path) -> None:
    s = draw(3, emitter="radiators")
    sim, writes = simulated(tmp_path, s, 2.0, optimizer=True)
    temps = indoor(sim, s.start_time, 1.0)
    inside = sum(20.0 <= t <= 22.5 for t in temps) / len(temps)
    assert inside >= 0.9, (min(temps), max(temps))
    assert floor_holds(sim, s.start_time)
    assert offsets(writes)  # it steered
    from collections import Counter

    assert max(per_day(writes, s.start_time)) <= 50, Counter((w[1], w[2]) for w in writes)
    # The optimizer drove, slot by slot, and the rules' decisions were kept beside.
    assert ("optimizer", 1) in PLANNED[tmp_path]
    assert ("rules", 0) in PLANNED[tmp_path]


def test_a_bath_too_hot_to_make_is_at_risk_then_missed(tmp_path: Path) -> None:
    """Hotter than the compressor can make the tank: at risk in the plan, told as an event,
    and missed at its latest, with how warm the water was."""
    s = draw(3, emitter="radiators")

    def bath() -> list[Intent]:
        now = clock.now()
        made = {"principal": "user:test", "created": now - timedelta(minutes=1)}
        return [kinds.bath(TANK, 75.0, now + timedelta(hours=8), **made)]  # type: ignore[arg-type]

    simulated(tmp_path, s, 0.5, optimizer=True, more=bath)

    async def read() -> tuple[list[Intent], list[Event]]:
        async with await Database.open(tmp_path / "sim.db") as db:
            ended = await Intents(db, AuditLog(tmp_path / "read-audit")).all(open_only=False)
            return ended, await Events(db).since(0.0)

    intents, events = asyncio.run(read())
    [found] = [i for i in intents if i.kind == "bath"]
    assert found.state == "missed"
    assert found.why is not None
    assert found.why.startswith("The hot water was ")
    assert found.why.endswith("at its latest, short of 75.0 °C")
    told = [e for e in events if e.key == f"intent.state:{found.id}"]
    assert told
    assert told[0].kind in ("intent.at_risk", "intent.giving_way")
    assert told[0].principal == "user:test"


def test_shadow_decides_as_the_optimizer_in_control(tmp_path: Path) -> None:
    shadow_equals_control(tmp_path, draw(1, emitter="radiators"), days=1.0, optimizer=True)


@pytest.mark.nightly
@pytest.mark.parametrize("seed", range(2, 8))
def test_the_planner_over_days(tmp_path: Path, seed: int) -> None:
    emitter: Emitter = "floor" if seed % 2 else "radiators"
    s = draw(seed, emitter=emitter, start=datetime(2026, 1, 10 + seed, tzinfo=UTC))
    sim, writes = simulated(tmp_path / "run", s, 4.0)
    temps = indoor(sim, s.start_time, 2.0)
    inside = sum(19.5 <= t <= 23.0 for t in temps) / len(temps)
    assert inside >= 0.9, (emitter, min(temps), max(temps))
    assert floor_holds(sim, s.start_time)
    assert max(per_day(writes, s.start_time)) <= 50
    shadow_equals_control(tmp_path / "pair", s, days=2.0)


# --- the optimizer against the rules ---------------------------------------------------------


class Gap(UserWarning):
    """Where the optimizer doesn't yet do better than the rules: reported, not failed."""


class Hindsight(UserWarning):
    """The optimizer told the truth beside it and the rules: what perfect information would
    be worth, reported every night."""


def scored(sim: Sim, s: Scenario, after_days: float) -> tuple[float, float]:
    """From `after_days` on: what the pump's electricity cost, and the rooms' degree-hours
    outside their band, as a person feels them (the hourly mean)."""
    prices = {p.start.timestamp(): p.value for p in day_ahead(s.seed, s.start_time, 4.0)}
    since = s.start_time.timestamp() + after_days * DAY_S
    rows = [row for row in sim.trace if row["t"] >= since]
    cost = 0.0
    for row, after in itertools.pairwise(rows):
        slot = row["t"] // 900 * 900
        cost += (after["pump_kwh"] - row["pump_kwh"]) * prices.get(slot, 0.0)
    level = LEVELS[0]
    assert level.low is not None
    assert level.high is not None
    outside = sum(
        (max(0.0, level.low - t) + max(0.0, t - level.high)) * 0.25
        for t in indoor(sim, s.start_time, after_days)
    )
    return cost, outside


@pytest.mark.nightly
@pytest.mark.filterwarnings("default::UserWarning")
@pytest.mark.parametrize("seed", range(2, 8))
def test_the_optimizer_against_the_rules(tmp_path: Path, seed: int) -> None:
    """On the same house, weather and prices, over the last two of three days: where the
    optimizer doesn't cost less than the rules at comfort as good, the gap is reported; a
    loss far beyond what it has shown fails, so a regression shows. Beside them, the
    optimizer told the truth, as the hindsight optimum: its gap is reported every night."""
    emitter: Emitter = "floor" if seed % 2 else "radiators"
    s = draw(seed, emitter=emitter, start=datetime(2026, 1, 10 + seed, tzinfo=UTC))
    ruled, _ = simulated(tmp_path / "rules", s, 3.0)
    optimized, _ = simulated(tmp_path / "optimizer", s, 3.0, optimizer=True)
    told, _ = simulated(tmp_path / "hindsight", s, 3.0, hindsight=True)
    rules_cost, rules_outside = scored(ruled, s, 1.0)
    cost, outside = scored(optimized, s, 1.0)
    best_cost, best_outside = scored(told, s, 1.0)
    gap = (
        f"seed {seed}, {emitter}: the rules {rules_cost:.2f} at {rules_outside:.2f} °C·h"
        f" outside the band, the optimizer {cost:.2f} at {outside:.2f} °C·h"
    )
    warnings.warn(
        Hindsight(f"{gap}, told the truth {best_cost:.2f} at {best_outside:.2f} °C·h"),
        stacklevel=1,
    )
    if cost > rules_cost or outside > rules_outside + 0.1:
        warnings.warn(Gap(f"the optimizer behind the rules: {gap}"), stacklevel=1)
    assert outside <= rules_outside + 5.0, gap
    assert cost <= rules_cost * 1.15, gap
