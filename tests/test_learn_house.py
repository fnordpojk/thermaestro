"""The house learned per climate system: against a house of known values, against the
simulated house (whose values the learner doesn't know), and the day-by-day reading."""

import math
import random
from datetime import UTC, datetime
from functools import cache
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from plant import begin, draw

from thermaestro.core import AuditLog, Values
from thermaestro.core.sensors import SITE
from thermaestro.core.values import Key, Sample
from thermaestro.learn import Learnings
from thermaestro.learn import house as h
from thermaestro.learn.heating import MODEL, RESPONSE, TAU, Heating
from thermaestro.planner.rules import _response_h
from thermaestro.store import Database, Room, Sensor

START = datetime(2026, 1, 12, tzinfo=UTC).timestamp()


def known(hours: int = 240, lag: int = 3, seed: int = 1) -> list[h.Hour]:
    """A house that is the model itself: alpha 0.3, beta 0.1, r 0.25 (a time constant of 40
    hours), sun and gains, the water's effect lagging `lag` hours, and the water varied
    by hand, as price shifting would."""
    rng = random.Random(seed)
    truth = h.Fit(0.3, 0.1, 0.02, 0.0, 0.5, 0.25, lag, 0, 0.0)
    out: list[h.Hour] = []
    mass = 21.0
    room = 21.0
    for k in range(hours):
        outdoor = 2 + 5 * math.sin(k / 30) + rng.uniform(-1, 1)
        water = 32 + 6 * math.sin(k / 7) + rng.uniform(-3, 3)
        sun = max(0.0, 300 * math.sin(math.pi * ((k % 24) - 8) / 8)) if 8 <= k % 24 <= 16 else 0
        hour = h.Hour(START + k * 3600, 0.0, outdoor, water, sun, 3.0)
        out.append(hour)
        mean = h.lagged(out, k, lag) or water
        if k:
            mass += truth.r * (room - mass)
        room = truth.room(mass, hour, mean)
        out[k] = h.Hour(hour.t, round(room, 3), outdoor, water, sun, 3.0)
    return out


def test_a_known_house_is_found_again() -> None:
    fit, why, _ = h.fit(known())
    assert why is None
    assert fit is not None
    assert fit.lag == 3
    assert fit.tau_h == pytest.approx(40, rel=0.15)


def test_a_known_house_proves_itself() -> None:
    hours = known()
    fit, verdict, _, _ = h.judge(hours, hours[-48].t)
    assert fit is not None
    assert verdict is not None
    assert verdict.proved, verdict


def test_heating_that_only_follows_the_weather_cant_be_told_apart() -> None:
    hours = []
    for k in range(200):
        outdoor = 3 + 6 * math.sin(k / 20)
        room = 21.0 + 0.4 * math.sin(k / 20)
        hours.append(h.Hour(START + k * 3600, room, outdoor, 35 - 0.8 * outdoor))
    fit, why, _ = h.fit(hours)
    assert fit is None
    assert why == h.TOGETHER


def test_rooms_that_hardly_moved_teach_nothing() -> None:
    hours = [
        h.Hour(START + k * 3600, 21.0 + 0.01 * (k % 3), 3 + 6 * math.sin(k / 20), 30 + (k % 7))
        for k in range(200)
    ]
    fit, why, _ = h.fit(hours)
    assert fit is None
    assert why == h.STILL


def test_too_few_hours_say_so() -> None:
    fit, why, args = h.fit(known(hours=40))
    assert fit is None
    assert why == h.FEW
    assert args["least"] == h.LEAST_HOURS


def test_hours_left_out_dont_count() -> None:
    clean = known()
    spoiled = [
        h.Hour(x.t, (x.room or 0) + 4, x.outdoor, x.water, x.sun, x.wind, "fireplace")
        if 100 <= i < 110
        else x
        for i, x in enumerate(clean)
    ]
    a, _, _ = h.fit(clean)
    b, _, _ = h.fit(spoiled)
    assert a is not None
    assert b is not None
    assert b.tau_h == pytest.approx(a.tau_h, rel=0.05)


@cache
def plant_hours(seed: int = 4, days: int = 12) -> tuple[Any, list[h.Hour]]:
    """The simulated house, an hour at a time, its heating offset moved every few hours as
    price shifting would: the learner doesn't know the plant's values."""
    scenario = draw(seed, start=datetime(2026, 1, 12, tzinfo=UTC), emitter="radiators", mean_c=0)
    plant = begin(scenario)
    rng = random.Random(seed)
    weather = plant._weather
    assert weather is not None
    out = []
    for hour in range(days * 24):
        if hour % 4 == 0:
            plant.registers[47011] = rng.randint(-3, 3) & 0xFFFF
        t0 = plant.t
        sums = [0.0, 0.0, 0.0]
        for _ in range(60):
            plant.run(60)
            sums[0] += plant.zones[0].air
            sums[1] += plant.outdoor
            sums[2] += plant.zones[0].emitter
        out.append(
            h.Hour(t0, sums[0] / 60, sums[1] / 60, sums[2] / 60, weather.sun(t0), weather.wind(t0))
        )
    return scenario, out


def test_the_simulated_house_is_learned_and_proves_itself() -> None:
    scenario, hours = plant_hours()
    fit, verdict, why, _ = h.judge(hours, hours[-48].t)
    assert fit is not None, why
    assert verdict is not None
    assert verdict.proved, verdict
    house = scenario.houses[0]
    tau = (house.c_air_j_k + house.c_mass_j_k) / house.ua_w_k / 3600
    assert fit.tau_h is not None
    assert tau / 3 < fit.tau_h < tau * 3, (fit.tau_h, tau)


def test_the_response_used_is_learned_once_proved() -> None:
    sit = cast(
        Any, SimpleNamespace(response_h={"dev:hp1/cs1": 2.0}, caps=SimpleNamespace(emitters={}))
    )
    assert _response_h(sit, "dev:hp1/cs1") == 2.0
    slab = SimpleNamespace(response_h={}, caps=SimpleNamespace(emitters={"dev:hp1/cs1": "slab"}))
    assert _response_h(cast(Any, slab), "dev:hp1/cs1") == 12.0


async def test_the_house_is_read_day_by_day_and_kept(tmp_path: Path) -> None:
    _, hours = plant_hours()
    node = SimpleNamespace
    described = node(
        nodes=[node(path="hp1", kind="unit"), node(path="hp1/cs1", kind="climate_system")],
        points=[],
    )
    host = cast(Any, node(instances={"dev": node(described=described)}))
    rows: list[tuple[Key, Sample]] = []
    for x in hours:
        for key, value in (
            (Key(SITE, "room.living/temperature"), x.room),
            (Key(SITE, "outdoor/temperature"), x.outdoor),
            (Key("dev", "hp1/cs1/supply.temp"), x.water),
        ):
            for m in range(0, 60, 10):
                rows.append((key, Sample(x.t + m * 60, value, None, "good")))
    end = hours[-1].t + 3600
    async with await Database.open(tmp_path / "t.db") as db:
        await db.put(Room(name="Living room", climate_system="dev:hp1/cs1"), "living")
        await db.put(Sensor(name="Living", source="mqtt", topic="l/t", room="living"), "living")
        await db.run(
            lambda t: t.executemany(
                "INSERT INTO history (instance, point, t, value, text, quality)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                [(k.instance, k.point, s.t, s.value, s.text, s.quality) for k, s in rows],
            )
        )
        learnings = Learnings(db, AuditLog(tmp_path / "audit"))
        await learnings.load()
        heating = Heating(
            db, Values(db), host, learnings, lambda key: key.point != "hp1/cs1/return.temp"
        )
        await heating.run(end)
        model = learnings.get(MODEL, "dev:hp1/cs1")
        assert model is not None
        assert model.value is not None, model
        assert learnings.get(TAU, "dev:hp1/cs1") is not None
        response = learnings.get(RESPONSE, "dev:hp1/cs1")
        assert response is not None
        assert response.starting == 4.0  # an unknown emitter's, until learned
        days = await heating._events("dev:hp1/cs1", 0.0)
        assert len(days) >= 11
        await heating.run(end)
        assert len(await heating._events("dev:hp1/cs1", 0.0)) == len(days)


def test_the_house_is_shown_in_words() -> None:
    from thermaestro.learn import Learned
    from thermaestro.web.learn_operations import _shown

    model = Learned(
        name=MODEL, scope="dev:hp1/cs1", kind="model", value={"error": 0.18, "lag": 3.0}
    )
    assert _shown(model) == "off by 0.18 °C on average six hours ahead"
    tau = Learned(name=TAU, scope="dev:hp1/cs1", kind="model", value=41.6, unit="h")
    assert _shown(tau) == "42 hours"


async def test_a_system_without_its_inputs_says_why(tmp_path: Path) -> None:
    node = SimpleNamespace
    described = node(
        nodes=[node(path="hp1", kind="unit"), node(path="hp1/cs1", kind="climate_system")],
        points=[],
    )
    host = cast(Any, node(instances={"dev": node(described=described)}))
    async with await Database.open(tmp_path / "t.db") as db:
        learnings = Learnings(db, AuditLog(tmp_path / "audit"))
        await learnings.load()
        heating = Heating(db, Values(db), host, learnings, lambda key: True)
        await heating.run(START + 86_400)
        found = learnings.get(MODEL, "dev:hp1/cs1")
        assert found is not None
        assert found.args == {"what": "a room temperature in this climate system"}
