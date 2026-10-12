"""The optimizer's plan, carried out in the simulated house: with what the learners made of
ten days of it, the rooms must follow what the plan predicted. The plant isn't the
optimizer's model: it has its own emitters, mass, hot-water charges and gains."""

import statistics
from datetime import UTC, datetime

from plant import begin, draw
from plant.hourly import Recorded, hour, shifted
from plant.optimizing import emitter_pairs

from thermaestro.learn import emitters, house, pump
from thermaestro.optimize import Band, Problem, Room, Slot, System, plan

START = datetime(2026, 1, 12, tzinfo=UTC)
PRICES = [0.4] * 6 + [1.2] * 3 + [0.9] * 7 + [1.8] * 4 + [1.0] * 4
"""Cheap at night, expensive in the morning and early evening, by the hour."""


def test_the_plan_comes_true_in_the_simulated_house() -> None:
    scenario = draw(4, start=START, emitter="radiators", mean_c=0)
    plant = begin(scenario)
    seen = shifted(plant, 10, 4)
    fit, why, _ = house.fit(seen.hours)
    assert fit is not None, why
    water, why, _ = pump.fit_water(seen.hours)
    assert water is not None, why
    emitted = emitters.fit(emitter_pairs(seen))
    assert emitted is not None
    k, c = emitted
    weather = plant._weather
    assert weather is not None
    slots = tuple(
        Slot(
            plant.t + 900 * j,
            price=PRICES[j // 4],
            outdoor=weather.outdoor(plant.t + 900 * j + 450),
            sun=weather.sun(plant.t + 900 * j + 450),
            wind=weather.wind(plant.t + 900 * j + 450),
            source=scenario.weather.ground_c - 1,
        )
        for j in range(96)
    )
    last = seen.hours[-1]
    system = System(
        scope="pump:hp1/cs1",
        fit=fit,
        water=water,
        emitters_kw_k=k,
        offset_low=-10,
        offset_high=10,
        offset_now=last.offset or 0.0,
        room=plant.zones[0].air,
        water_before=tuple(x.water or 0.0 for x in seen.hours[-24:]),
        rooms=(Room("room:r1", 0.0, tuple(Band(20.5, 22.0) for _ in range(24))),),
        share=0.5,
        draw_kw=statistics.median(seen.drawn_kw),
        emitters_kwh_k=c,
    )
    found = plan(Problem(slots, systems=(system,), slider=0.7))
    assert found.ok, found.why
    planned = found.systems[system.scope]
    after = Recorded([], [], [])
    for h in range(24):
        hour(plant, round(planned.offset[h]), after)
    actual = [x.room or 0.0 for x in after.hours]
    misses = [abs(a - p) for a, p in zip(actual, planned.room, strict=True)]
    assert statistics.mean(misses) < 0.5, (actual, planned.room)
    # The plan has no tank here, while the plant charges its hot water, taking turns with
    # the heating: the rooms may dip a little below what was planned for them.
    assert min(actual) > 19.5
    assert max(actual) < 22.5
