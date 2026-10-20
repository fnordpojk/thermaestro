"""The house learned per climate system: against a house of known values, against the
simulated house (whose values the learner doesn't know), and the day-by-day reading."""

import math
import random
from dataclasses import replace
from datetime import UTC, datetime
from functools import cache
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from plant import begin, draw
from plant.model import FLOW_W_K, curve_for

from thermaestro.core import AuditLog, Values
from thermaestro.core.sensors import SITE
from thermaestro.core.values import Key, Sample
from thermaestro.core.weather import Forecast, Span
from thermaestro.intents import Capabilities
from thermaestro.learn import Learned, Learnings, pump
from thermaestro.learn import house as h
from thermaestro.learn.forecast import BIAS
from thermaestro.learn.heating import MODEL, PUMP, RESPONSE, TAU, Heating
from thermaestro.planner import Planner
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
    offset = 0
    for hour in range(days * 24):
        if hour % 4 == 0:
            offset = rng.randint(-3, 3)
            plant.registers[47011] = offset & 0xFFFF
        t0 = plant.t
        sums = [0.0, 0.0, 0.0, 0.0, 0.0]
        for _ in range(60):
            plant.run(60)
            zone = plant.zones[0]
            sums[0] += zone.air
            sums[1] += plant.outdoor
            sums[2] += zone.emitter
            sums[3] += zone.emitter + zone.heat_w / (2 * FLOW_W_K)
            sums[4] += plant.compressor_on and plant.demand == "heating"
        out.append(
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


def test_the_pumps_curve_is_learned_and_proves_itself() -> None:
    scenario, hours = plant_hours()
    found, verdict, why, _ = pump.judge(hours, hours[-pump.TEST_HOURS].t)
    assert found is not None, why
    assert verdict is not None
    assert verdict.proved, verdict
    curve = curve_for(scenario.houses[0])
    assert found.c1 == pytest.approx(-(0.09 * curve + 0.06), rel=0.5)
    assert found.c2 == pytest.approx(1 + 0.15 * curve, rel=0.5)


def test_the_curve_from_the_pumps_calculated_supply_however_short_the_runs() -> None:
    """A quarter of an hour's heating each hour, the curve's own supply reported: learned."""
    hours = []
    for i in range(96):
        outdoor = 5.0 + 4.0 * math.sin(i / 7)
        offset = 0.0 if (i // 24) % 2 == 0 else 2.0
        target = 30.0 - 0.5 * outdoor + 1.5 * offset
        hours.append(
            h.Hour(
                START + i * 3600, 21.0, outdoor, 30.0, offset=offset, heating=0.25, target=target
            )
        )
    found, why, _ = pump.fit(hours)
    assert found is not None, why
    assert found.c1 == pytest.approx(-0.5)
    assert found.c2 == pytest.approx(1.5)
    measured = [replace(x, target=None, supply=40.0) for x in hours]
    assert pump.fit(measured)[1] == pump.FEW  # never heated half an hour


def test_an_offset_that_never_moved_teaches_nothing() -> None:
    _, hours = plant_hours()
    still = [
        h.Hour(x.t, x.room, x.outdoor, x.water, x.sun, x.wind, x.skip, x.supply, 0, x.heating)
        for x in hours
    ]
    found, why, _ = pump.fit(still)
    assert found is None
    assert why == pump.STILL


def test_colder_weather_ahead_raises_the_offset_now() -> None:
    curve = pump.Response(40.0, -0.5, 1.5, 100)
    assert pump.lead(curve, 0.0, -6.0, 3) == 2
    assert pump.lead(curve, 0.0, 6.0, 3) == -2
    assert pump.lead(curve, 0.0, -30.0, 3) == 3
    assert pump.lead(curve, 0.0, 0.4, 3) == 0


async def test_the_planner_leads_only_a_proved_curve_without_a_room_sensor(
    tmp_path: Path,
) -> None:
    async def forecast(quantity: str, start: datetime, end: datetime) -> Forecast:
        return Forecast(quantity, "met", spans=[Span(start, end, -8.0)])

    weather = cast(Any, SimpleNamespace(forecast=forecast))
    caps = Capabilities(
        systems=frozenset({"dev:hp1/cs1", "dev:hp1/cs2"}),
        rooms={"room:living": "dev:hp1/cs2"},
        emitters={"dev:hp1/cs1": "slab", "dev:hp1/cs2": "slab"},
    )
    now = datetime.fromtimestamp(START, UTC)
    async with await Database.open(tmp_path / "t.db") as db:
        audit = AuditLog(tmp_path / "audit")
        learnings = Learnings(db, audit)
        await learnings.load()
        nothing = cast(Any, None)
        planner = Planner(
            db,
            Values(db),
            nothing,
            nothing,
            nothing,
            nothing,
            audit,
            learnings=learnings,
            weather=weather,
        )
        curve = {"c0": 30.0, "c1": -0.6, "c2": 1.8}
        for scope in ("dev:hp1/cs1", "dev:hp1/cs2"):
            await learnings.put(
                Learned(name=PUMP, scope=scope, kind="model", value=curve, state="learning")
            )
        assert await planner._leads(caps, now, 0.0) == {}  # not proved yet
        for scope in ("dev:hp1/cs1", "dev:hp1/cs2"):
            await learnings.put(
                Learned(name=PUMP, scope=scope, kind="model", value=curve, state="proved")
            )
        found = await planner._leads(caps, now, 0.0)
        assert list(found) == ["dev:hp1/cs1"]  # the other has a room sensor
        steps, ahead, hours = found["dev:hp1/cs1"]
        assert (steps, ahead) == (3, -8.0)
        assert hours >= 2
        # A forecast that runs a degree warm, once that is proved, is taken a degree down.
        warm = {"1": 1.0, "6": 1.0, "24": 1.0, "48": 1.0}
        await learnings.put(
            Learned(name=BIAS, scope="met:temperature", kind="model", value=warm, state="proved")
        )
        assert (await planner._leads(caps, now, 0.0))["dev:hp1/cs1"][1] == -9.0


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
        curve = learnings.get(PUMP, "dev:hp1/cs1")
        assert curve is not None
        assert curve.why == pump.FEW  # no heating offset or compressor read here
        days = await heating._events("dev:hp1/cs1", 0.0)
        assert len(days) >= 11
        await heating.run(end)
        assert len(await heating._events("dev:hp1/cs1", 0.0)) == len(days)


async def test_a_system_without_a_room_sensor_learns_its_pumps_curve(tmp_path: Path) -> None:
    _, hours = plant_hours()
    node = SimpleNamespace
    described = node(
        nodes=[
            node(path="hp1", kind="unit"),
            node(path="hp1/cs1", kind="climate_system"),
            node(path="hp1/compressor.ep14", kind="compressor"),
        ],
        points=[node(path="hp1/compressor.ep14/state")],
        levers=[node(path="hp1/cs1/heating.offset", verify=node(point="hp1/cs1/x.nibe.47011"))],
    )
    host = cast(Any, node(instances={"dev": node(described=described)}))

    def at(point: str, t: float, value: float | None, text: str | None = None) -> None:
        instance = SITE if point.startswith("outdoor/") else "dev"
        rows.append((Key(instance, point), Sample(t, value, text, "good")))

    rows: list[tuple[Key, Sample]] = []
    for x in hours:
        assert x.supply is not None
        assert x.heating is not None
        for m in range(0, 60, 10):
            at("outdoor/temperature", x.t + m * 60, x.outdoor)
            at("hp1/cs1/supply.temp", x.t + m * 60, x.supply)
        at("hp1/cs1/x.nibe.47011", x.t, x.offset)
        if x.heating:
            at("hp1/compressor.ep14/state", x.t, None, "running")
            at("hp1/demand", x.t, None, "heating")
        off = x.t + round(x.heating * 60) * 60
        if off < x.t + 3600:
            at("hp1/compressor.ep14/state", off, None, "stopped")
            at("hp1/demand", off, None, "idle")
    async with await Database.open(tmp_path / "t.db") as db:
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
        await heating.run(hours[-1].t + 3600)
        curve = learnings.get(PUMP, "dev:hp1/cs1")
        assert curve is not None
        assert curve.state == "proved", curve
        model = learnings.get(MODEL, "dev:hp1/cs1")
        assert model is not None
        assert model.args == {"what": "a room temperature in this climate system"}


def test_the_house_is_shown_in_words() -> None:
    from thermaestro.web.learn_operations import _shown

    model = Learned(
        name=MODEL, scope="dev:hp1/cs1", kind="model", value={"error": 0.18, "lag": 3.0}
    )
    assert _shown(model) == "off by 0.18 °C on average six hours ahead"
    tau = Learned(name=TAU, scope="dev:hp1/cs1", kind="model", value=41.6, unit="h")
    assert _shown(tau) == "42 hours"
    curve = Learned(
        name=PUMP, scope="dev:hp1/cs1", kind="model", value={"c0": 30, "c1": -0.6, "c2": 1.8}
    )
    assert _shown(curve) == "1.8 °C of supply per offset step, 0.60 °C per degree colder"


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


def test_an_offset_that_never_moved_takes_the_documented_step() -> None:
    """Nothing moves the offset: what a step does is the maker's figure, and the curve's
    slope is learned and judged against a supply that doesn't follow the weather."""
    hours = [
        h.Hour(
            START + i * 3600,
            21.0,
            outdoor,
            30.0,
            offset=1.0,
            heating=0.25,
            target=30.0 - 0.5 * outdoor + 2.5,
        )
        for i in range(200)
        for outdoor in [5.0 + 6.0 * math.sin(i / 9)]
    ]
    assert pump.fit(hours)[1] == pump.STILL
    found, why, _ = pump.fit(hours, step=2.5)
    assert found is not None, why
    assert found.documented
    assert found.c0 == pytest.approx(30.0)
    assert found.c1 == pytest.approx(-0.5)
    assert found.c2 == 2.5
    judged, verdict, _, _ = pump.judge(hours, hours[-pump.TEST_HOURS].t, step=2.5)
    assert judged is not None
    assert verdict is not None
    assert verdict.proved, verdict


async def test_an_hour_without_the_outdoor_reference_takes_the_pumps_sensor(
    tmp_path: Path,
) -> None:
    """The outdoor reference away for days (its Home Assistant down): those hours take the
    pump's own outdoor sensor rather than being lost."""
    _, hours = plant_hours()
    node = SimpleNamespace
    described = node(
        nodes=[node(path="hp1", kind="unit"), node(path="hp1/cs1", kind="climate_system")],
        points=[],
    )
    host = cast(Any, node(instances={"dev": node(described=described)}))
    half = hours[len(hours) // 2].t
    rows: list[tuple[Key, Sample]] = []
    for x in hours:
        for m in range(0, 60, 10):
            t = x.t + m * 60
            rows.append((Key(SITE, "room.living/temperature"), Sample(t, x.room, None, "good")))
            rows.append((Key("dev", "hp1/cs1/supply.temp"), Sample(t, x.water, None, "good")))
            rows.append((Key("dev", "hp1/outdoor.temp"), Sample(t, x.outdoor, None, "good")))
            if x.t < half:
                rows.append((Key(SITE, "outdoor/temperature"), Sample(t, x.outdoor, None, "good")))
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
        await heating.run(hours[-1].t + 3600)
        kept = [h for _, body in await heating._events("dev:hp1/cs1", 0.0) for h in body["hours"]]
        late = [h for h in kept if h["t"] >= half + 3 * 3600]
        assert late
        assert all(h["outdoor"] is not None for h in late)
