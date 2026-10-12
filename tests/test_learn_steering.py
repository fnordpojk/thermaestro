"""What the optimizer steers by, learned against the simulated house: how the system's
water follows the offset, what the emitters give per degree, and a pool."""

import random
from datetime import UTC, datetime
from functools import cache
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from plant import begin, draw
from plant.model import FLOW_W_K

from thermaestro.core import AuditLog, Values
from thermaestro.learn import Learned, Learnings, pump
from thermaestro.learn import emitters as e
from thermaestro.learn import house as h
from thermaestro.learn import pool as p
from thermaestro.learn.efficiency import HEATING
from thermaestro.learn.emitters import EMITTERS, Emitters
from thermaestro.learn.pool import POOL, Pools
from thermaestro.store import Database

START = datetime(2026, 1, 12, tzinfo=UTC)


@cache
def heated(seed: int = 4, days: int = 10) -> tuple[Any, list[h.Hour], list[float]]:
    """The simulated house an hour at a time, its offset moved every few hours as price
    shifting would; and the heat the pump gave each hour, which nothing on a real pump
    measures."""
    scenario = draw(seed, start=START, emitter="radiators", mean_c=0)
    plant = begin(scenario)
    rng = random.Random(seed)
    weather = plant._weather
    assert weather is not None
    hours, heat = [], []
    offset = 0
    for hour in range(days * 24):
        if hour % 4 == 0:
            offset = rng.randint(-3, 3)
            plant.registers[47011] = offset & 0xFFFF
        t0, given = plant.t, plant.heat["heating"]
        sums = [0.0] * 5
        for _ in range(60):
            plant.run(60)
            zone = plant.zones[0]
            sums[0] += zone.air
            sums[1] += plant.outdoor
            sums[2] += zone.emitter
            sums[3] += zone.emitter + zone.heat_w / (2 * FLOW_W_K)
            sums[4] += plant.compressor_on and plant.demand == "heating"
        hours.append(
            h.Hour(
                t0,
                sums[0] / 60,
                sums[1] / 60,
                sums[2] / 60,
                weather.sun(t0),
                weather.wind(t0),
                None,
                sums[3] / 60,
                offset,
                sums[4] / 60,
            )
        )
        heat.append(plant.heat["heating"] - given)
    return scenario, hours, heat


def test_the_water_follows_the_offset_and_proves_itself() -> None:
    _, hours, _ = heated()
    found, verdict, why, _ = pump.judge_water(hours, hours[-pump.TEST_HOURS].t)
    assert found is not None, why
    assert verdict is not None
    assert verdict.proved, verdict
    assert found.w2 > 0.5  # a step warms the water
    assert found.w1 < 0  # colder outside, warmer water
    # While the compressor heats, the supply runs warmer than the water by half the spread.
    assert 0.5 < found.lift < 5


def test_the_water_needs_the_offset_to_have_moved() -> None:
    _, hours, _ = heated()
    still = [
        h.Hour(x.t, x.room, x.outdoor, x.water, x.sun, x.wind, x.skip, x.supply, 0, x.heating)
        for x in hours
    ]
    found, why, _ = pump.fit_water(still)
    assert (found, why) == (None, pump.STILL)


def test_the_emitters_output_is_found_again() -> None:
    scenario, hours, heat = heated()
    pairs = [
        e.Pair(x.t, (x.water or 0) - (x.room or 0), q)
        for x, q in zip(hours, heat, strict=True)
        if x.heating and x.heating > 0.15
    ]
    k, verdict = e.judge(pairs, hours[-e.TEST_HOURS].t)
    assert k is not None
    assert verdict is not None
    assert verdict.proved, verdict
    # The plant's radiators give their conductance times how far they are over the air.
    assert k == pytest.approx(scenario.houses[0].ua_emitter_w_k / 1000, rel=0.25)


def test_too_few_hours_give_no_output() -> None:
    assert e.fit([e.Pair(0.0, 10.0, 4.0)] * (e.LEAST - 1)) is None
    assert e.fit([e.Pair(0.0, 0.2, 4.0)] * 50) is None  # the water hardly over the room


def _ns(**kw: Any) -> Any:
    return SimpleNamespace(**kw)


async def test_the_emitters_are_learned_from_the_kept_hours(tmp_path: Path) -> None:
    """From the house's hours and the COP's heating hours, as the learners keep them."""
    import json

    rng = random.Random(3)
    t0 = START.timestamp()
    house_hours, runs = [], []
    for i in range(10 * 24):
        t = t0 + i * 3600
        warmer = rng.uniform(5, 15)
        house_hours.append({"t": t, "room": 21.0, "water": 21.0 + warmer, "skip": None})
        # 0.4 kW/K: the heat is the metered electricity times a COP of half the ideal.
        supply, source = 21 + warmer + 2, 4.0
        ideal = (supply + 273.15) / (supply - source)
        kwh = 0.4 * warmer / (0.5 * ideal)
        body = {"run_s": 1800, "supply_c": supply, "source_c": source, "brine_ks": 0, "kwh": kwh}
        runs.append((t, body))
    async with await Database.open(tmp_path / "t.db") as db:
        rows = [("dev:hp1/cs1", "house_day", t0, json.dumps({"day": "x", "hours": house_hours}))]
        rows += [("dev:hp1", "cop_hour", t, json.dumps(b)) for t, b in runs]
        await db.run(
            lambda tr: tr.executemany(
                "INSERT INTO learn_events (scope, kind, t, body) VALUES (?, ?, ?, ?)", rows
            )
        )
        learnings = Learnings(db, AuditLog(tmp_path / "audit"))
        await learnings.load()
        await learnings.put(
            Learned(name=HEATING, scope="dev:hp1", kind="model", value=0.5, state="proved")
        )

        async def systems() -> list[Any]:
            return [_ns(scope="dev:hp1/cs1", rooms=["r1"])]

        service = Emitters(db, cast(Any, None), learnings, systems)
        await service.run(t0 + 10 * 86_400)
        found = learnings.get(EMITTERS, "dev:hp1/cs1")
        assert found is not None
        assert found.state == "proved"
        assert found.value == pytest.approx(0.4, rel=0.01)

        async def two() -> list[Any]:
            return [_ns(scope=f"dev:hp1/cs{i}", rooms=["r1"]) for i in (1, 2)]

        await Emitters(db, cast(Any, None), learnings, two).run(t0 + 10 * 86_400)
        found = learnings.get(EMITTERS, "dev:hp1/cs2")
        assert found is not None
        assert found.why == e.SEVERAL


# --- the pool ----------------------------------------------------------------------------

POOL_POINTS = {
    "temp": "hp1/pool1/temp",
    "demand": "hp1/demand",
    "state": "hp1/compressor.ep14/state",
    "outdoor": "hp1/outdoor.temp",
}


@cache
def pooled(seed: int = 5, days: int = 14) -> tuple[Any, list[Any], float]:
    """A mild fortnight with a pool: a reading a minute."""
    scenario = draw(seed, start=START, pool=True, mean_c=12)
    plant = begin(scenario)
    samples: list[Any] = []
    for _ in range(days * 24 * 60):
        plant.run(60)
        t = plant.t
        assert plant.pool is not None
        samples += [
            ("dev", POOL_POINTS["temp"], t, round(plant.pool, 2), None, "good"),
            ("dev", POOL_POINTS["demand"], t, None, plant.demand, "good"),
            (
                "dev",
                POOL_POINTS["state"],
                t,
                None,
                "running" if plant.compressor_on else "x",
                "good",
            ),
            ("dev", POOL_POINTS["outdoor"], t, round(plant.outdoor, 2), None, "good"),
        ]
    return scenario, samples, plant.t


def pool_host() -> Any:
    described = _ns(
        nodes=[
            _ns(path="hp1", kind="unit"),
            _ns(path="hp1/compressor.ep14", kind="compressor"),
            _ns(path="hp1/pool1", kind="pool"),
        ],
        points=[_ns(path=x) for x in POOL_POINTS.values()],
    )
    return _ns(instances={"dev": _ns(described=described)})


async def test_a_pool_is_learned_and_proves_itself(tmp_path: Path) -> None:
    scenario, samples, end = pooled()
    async with await Database.open(tmp_path / "t.db") as db:
        await db.run(
            lambda tr: tr.executemany(
                "INSERT INTO history (instance, point, t, value, text, quality)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                samples,
            )
        )
        learnings = Learnings(db, AuditLog(tmp_path / "audit"))
        await learnings.load()
        # The pump's own outdoor sensor: the site has no outdoor reference here.
        pools = Pools(db, Values(db), pool_host(), learnings, lambda key: key.instance == "dev")
        await pools.run(end)
        found = learnings.get(POOL, "dev:hp1/pool1")
        assert found is not None
        assert found.state == "proved", found.why
        assert isinstance(found.value, dict)
        pool = scenario.pool
        water = 4186.0 * pool.m3 * 1000
        assert found.value["b"] == pytest.approx(pool.loss_w_k * 3600 / water, rel=0.35)
        # The compressor's heat on the pool: some 6 kW, so a few tenths of a degree an hour.
        assert 0.05 < found.value["a"] < 0.5
        # Read once: the days are kept, not read again.
        await pools.run(end)
        assert learnings.get(POOL, "dev:hp1/pool1") == found


async def test_a_pool_without_its_inputs_says_why(tmp_path: Path) -> None:
    async with await Database.open(tmp_path / "t.db") as db:
        learnings = Learnings(db, AuditLog(tmp_path / "audit"))
        await learnings.load()
        await Pools(db, Values(db), pool_host(), learnings, lambda key: False).run(2e9)
        found = learnings.get(POOL, "dev:hp1/pool1")
        assert found is not None
        assert (found.state, found.why) == ("starting", p.MISSING)


def test_too_few_pool_hours_say_so() -> None:
    hours = [p.Hour(i * 3600.0, 25.0, 5.0, 0.0) for i in range(10)]
    found, why, _ = p.fit(hours)
    assert (found, why) == (None, p.FEW)
