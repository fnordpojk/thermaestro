"""The plant on its own: a house that behaves like one, a tank and pool that the pump's
settings govern, and simulated time that runs days in seconds."""

import asyncio
import statistics
import time
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest
from plant import Draw, Plant, Pool, Scenario, Weather, begin, draw
from plant.model import s16, signed
from plant.vloop import Stalled, simulate

from thermaestro import clock

STILL = Weather(mean_c=-5, swing_c=0, drift_c=0, clear=0, wind_m_s=0)
"""Weather that hardly changes, so one setting's effect shows."""
WARM = Weather(mean_c=22, swing_c=0, drift_c=0, clear=0, wind_m_s=0)
"""Above the heating stop: only hot water and the pool."""


def hours(plant: Plant, n: float, every_s: float = 60.0) -> list[dict[str, float | str | bool]]:
    """Run `n` hours, noting the state every `every_s`."""
    rows: list[dict[str, float | str | bool]] = []
    for _ in range(round(n * 3600 / every_s)):
        plant.run(every_s)
        rows.append(
            {
                "t": plant.t,
                "air": plant.indoor(),
                "top": plant.top,
                "bottom": plant.bottom,
                "demand": plant.demand,
                "on": plant.compressor_on,
                "dm": plant.dm,
                "add": plant.addition_kw,
                "pool": plant.pool or 0.0,
                "kw": plant.house_kw,
                "pump_kw": plant.pump_kw,
            }
        )
    return rows


def test_a_scenario_comes_from_its_seed_and_is_kept_as_data(tmp_path: Path) -> None:
    assert draw(3) == draw(3)
    assert draw(3) != draw(4)
    s = draw(5, systems=2, pool=True)
    s.save(tmp_path / "s.json")
    assert Scenario.read(tmp_path / "s.json") == s
    for house in s.houses:
        assert 120 <= house.ua_w_k <= 280
    floor = draw(5, emitter="floor").houses[0]
    radiators = draw(5, emitter="radiators").houses[0]
    assert floor.c_emitter_j_k > 40 * radiators.c_emitter_j_k


@pytest.mark.parametrize("emitter", ["radiators", "floor"])
@pytest.mark.parametrize("seed", [0, 1, 2])
def test_the_house_settles_near_comfort(seed: int, emitter: str) -> None:
    plant = begin(draw(seed, emitter=emitter))  # type: ignore[arg-type]
    rows = hours(plant, 72)
    last_day = [float(r["air"]) for r in rows[-1440:]]
    # A house left to its curve: near comfort, not held in a band; that's the planner's.
    assert 18.5 <= statistics.mean(last_day) <= 23.5
    assert max(last_day) - min(last_day) < 5.0


@pytest.mark.nightly
@pytest.mark.parametrize("emitter", ["radiators", "floor"])
@pytest.mark.parametrize("seed", range(3, 40))
def test_houses_settle_over_many_seeds(seed: int, emitter: str) -> None:
    test_the_house_settles_near_comfort(seed, emitter)


def response_hours(emitter: str) -> float:
    """Hours until the indoor temperature is 0.5 °C up after the offset goes up by 3."""
    plant = begin(quiet(draw(11, emitter=emitter)))  # type: ignore[arg-type]
    hours(plant, 48)
    before = statistics.mean(float(r["air"]) for r in hours(plant, 2))
    plant.write(47011, 3)
    for row in hours(plant, 72):
        if float(row["air"]) >= before + 0.5:
            return (float(row["t"]) - plant.t) / 3600 + 72
    return float("inf")


def test_a_slab_answers_much_slower_than_radiators() -> None:
    radiators, floor = response_hours("radiators"), response_hours("floor")
    assert radiators < 8
    assert floor > 2 * radiators


def quiet(s: Scenario, weather: Weather = STILL) -> Scenario:
    """Steady weather, no draws, no periodic hot-water increase: one thing at a time."""
    return replace(s, weather=weather, draws=()).set({47050: 0})


def tank_only() -> Plant:
    """A plant above its heating stop, with one big draw at 01:00."""
    s = quiet(draw(2, start=datetime(2026, 5, 1, tzinfo=UTC)), WARM)
    return begin(replace(s, draws=(Draw(hour=1.0, liters=70.0, minutes=8.0),)))


def test_hot_water_charges_between_the_modes_start_and_stop() -> None:
    plant = tank_only()
    rows = hours(plant, 6, every_s=10)
    charging = [r for r in rows if r["demand"] == "dhw"]
    assert charging
    first = rows.index(charging[0])
    assert float(rows[first]["bottom"]) <= 45.0
    after = rows[rows.index(charging[-1]) + 1]
    assert float(after["bottom"]) >= 50.0 - 0.1
    assert all(r["demand"] in ("dhw", "idle") for r in rows)  # no heating above its stop


def test_a_low_start_holds_off_charges_until_it_is_reached() -> None:
    plant = tank_only()
    plant.write(47044, s16(25.0))
    rows = hours(plant, 6)
    assert min(float(r["bottom"]) for r in rows) < 45  # below the normal start, no charge
    assert not [r for r in rows if r["demand"] == "dhw" and float(r["bottom"]) > 25.5]


def test_a_one_time_increase() -> None:
    plant = tank_only()
    plant.write(48132, 4)
    rows = hours(plant, 0.5)
    assert rows[0]["demand"] == "dhw"
    while plant.word(48132):
        plant.run(60)
    assert plant.bottom >= 53.0 - 0.1
    assert plant.word(48132) == 0


def test_degree_minutes_start_and_stop_the_compressor() -> None:
    plant = begin(quiet(draw(4, emitter="radiators")))
    rows = hours(plant, 24, every_s=10)
    starts = [i for i in range(1, len(rows)) if rows[i]["on"] and not rows[i - 1]["on"]]
    stops = [i for i in range(1, len(rows)) if rows[i - 1]["on"] and not rows[i]["on"]]
    assert len(starts) > 2
    assert len(stops) > 2
    for i in starts:
        assert float(rows[i - 1]["dm"]) <= -60 + 1
    for i in stops:
        assert float(rows[i - 1]["dm"]) >= -1
    for start in starts:
        before = [stop for stop in stops if stop < start]
        if before:  # at least 20 minutes between a stop and the next start
            assert (start - before[-1]) * 10 >= 1200 - 10


def cold(registers: dict[int, int]) -> Plant:
    """A house too big for its pump on a very cold day: the addition is needed."""
    freezing = Weather(mean_c=-22, swing_c=0, drift_c=0, clear=0, wind_m_s=6)
    s = quiet(replace(draw(6, emitter="radiators"), heat_kw=4.0), freezing)
    return begin(s.set(registers))


def test_the_addition_keeps_to_its_stop_and_its_power() -> None:
    plant = cold({47212: 300})
    rows = hours(plant, 24)
    assert max(float(r["add"]) for r in rows) == pytest.approx(3.0)
    plant = cold({47376: s16(-25.0)})
    assert max(float(r["add"]) for r in hours(plant, 24)) == 0.0


def test_no_heating_above_the_heating_stop() -> None:
    plant = tank_only()
    rows = hours(plant, 24)
    assert all(r["demand"] != "heating" for r in rows)
    assert all(float(r["dm"]) == 0 for r in rows)


def test_the_pool_heats_between_its_start_and_stop_and_can_be_switched_off() -> None:
    s = replace(quiet(draw(8, pool=True), WARM), pool=Pool(m3=5.0, loss_w_k=400.0, start_c=21.0))
    plant = begin(s)
    rows = hours(plant, 24)
    assert [r for r in rows if r["demand"] == "pool"]
    assert max(float(r["pool"]) for r in rows) <= 28.0 + 0.2
    plant.write(48094, 0)
    assert all(r["demand"] != "pool" for r in hours(plant, 24))


def test_the_meter_counts_the_pump_and_the_household() -> None:
    plant = begin(draw(9))
    rows = hours(plant, 24)
    base = plant.scenario.load.base_kw
    assert all(float(r["kw"]) >= base - 1e-9 for r in rows)
    running = [float(r["pump_kw"]) for r in rows if r["on"] and float(r["add"]) == 0]
    heat = plant.scenario.heat_kw
    assert running
    assert all(heat / 6.5 <= kw <= heat / 1.4 for kw in running)


def test_the_registers_follow() -> None:
    plant = begin(draw(10))
    hours(plant, 3)
    r = plant.registers
    assert signed(r[40004]) / 10 == pytest.approx(plant.outdoor, abs=0.05)
    assert signed(r[40013]) / 10 == pytest.approx(plant.top, abs=0.05)
    assert signed(r[40014]) / 10 == pytest.approx(plant.bottom, abs=0.05)
    assert signed(r[43005]) / 10 == pytest.approx(plant.dm, abs=0.05)
    assert r[43086] == {"idle": 10, "dhw": 20, "heating": 30, "pool": 40}[plant.demand]
    assert r[40007] == 0x8000  # no second climate system: its supply sensor isn't there


def test_writes_as_the_pump_takes_them() -> None:
    plant = begin(draw(1))
    plant.refuse.add(47011)
    plant.not_kept.add(47041)
    assert not plant.write(47011, 2)
    assert plant.write(47041, 0)
    assert plant.word(47041) == 1
    assert plant.write(47376, s16(30.0))  # above the heating stop (17 °C)
    assert signed(plant.word(47376)) == 170


# --- simulated time ----------------------------------------------------------------------


def test_days_pass_in_moments() -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)

    async def main() -> tuple[datetime, datetime]:
        await asyncio.sleep(3 * 86_400)
        before = clock.now()
        await asyncio.to_thread(time.sleep, 0.05)  # real work in a thread: the clock waits
        return before, clock.now()

    began = time.perf_counter()
    before, after = simulate(main, start)
    assert time.perf_counter() - began < 2
    assert (before - start).total_seconds() == pytest.approx(3 * 86_400)
    assert after == before
    assert clock.now() > datetime(2026, 6, 1, tzinfo=UTC)  # the real clock again


def test_a_simulation_that_waits_forever_says_so() -> None:
    async def main() -> None:
        await asyncio.Event().wait()

    with pytest.raises(Stalled):
        simulate(main, datetime(2026, 1, 1, tzinfo=UTC))
