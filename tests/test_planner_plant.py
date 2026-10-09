"""The planner against the plant, over simulated days: the behavior contracts that need a
planner. Comfort and the hot-water floor hold, extreme prices buy neither cold nor heat,
shifting nets out, the write budget holds, the heating stop is followed, a lost room sensor
puts the offset back, a value not kept isn't asked for again, and shadow decides as
control would."""

import itertools
import json
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from plant import Plant, Scenario, draw
from plant.harness import PLANT, Frame, Sim, running
from plant.market import day_ahead
from plant.planning import CS, TANK, intended, planning
from plant.scenario import Emitter
from plant.vloop import simulate

from thermaestro import clock
from thermaestro.intents import RANKS, Intent, Level, kinds
from thermaestro.store import LeverMode, Plugin

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
) -> tuple[Sim, Intended]:
    """Run the planner on the plant for `days`; the sim, and the planner's intended writes."""

    path.mkdir(parents=True, exist_ok=True)

    async def body() -> tuple[Sim, Intended]:
        sim = Sim(s, path, levers or LEVERS, record=record, replay=replay)
        async with running(sim):
            series = day_ahead(s.seed, s.start_time, days, spike=spike, negative=negative)
            intents = household(slider=slider, rooms=rooms)
            async with planning(sim, intents, LEVELS, series, rooms=rooms):
                if during is not None:
                    await during(sim)
                await sim.run(days - (clock.time() - s.start_time.timestamp()) / DAY_S)
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


def test_extreme_prices_buy_neither_cold_nor_heat(tmp_path: Path) -> None:
    s = draw(4, emitter="radiators")
    sim, _ = simulated(tmp_path, s, 2.0, slider=1.0, spike=25.0, negative=True)
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


def test_the_heating_stop_leaves_heating_to_the_pump(tmp_path: Path) -> None:
    s = draw(6, emitter="radiators", mean_c=25.0)
    _, writes = simulated(tmp_path, s, 1.5)
    assert offsets(writes) == []


def test_a_lost_room_sensor_puts_the_offset_back(tmp_path: Path) -> None:
    s = draw(7, emitter="radiators")
    stopped: list[float] = []

    async def silence(sim: Sim) -> None:
        await sim.run(1.0)
        await sim.db.put(Plugin(plugin="plant_sensors", enabled=False), PLANT)
        await sim.host.apply(PLANT)
        stopped.append(clock.time())

    sim, writes = simulated(tmp_path, s, 1.5, slider=1.0, spike=3.0, during=silence)
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


def shadow_equals_control(path: Path, s: Scenario, days: float) -> None:
    controls: dict[str, LeverMode] = dict.fromkeys(LEVERS, "control")
    shadows: dict[str, LeverMode] = dict.fromkeys(LEVERS, "shadow")
    done, wanted = simulated(path / "control", s, days, levers=controls, record=True)
    copied, shadowed = simulated(
        path / "shadow",
        s,
        days,
        levers=shadows,
        replay=done.frames,
    )
    assert len(wanted) >= 4 * days
    assert shadowed == wanted
    assert copied.bus.writes == []


def test_shadow_decides_as_the_planner_in_control(tmp_path: Path) -> None:
    shadow_equals_control(tmp_path, draw(1, emitter="radiators"), days=1.0)


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
