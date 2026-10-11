"""A heat pump's own electricity meter on its supply: a pulse counter over MQTT or a Home
Assistant entity, checked, and the draw, the day's energy and each charge's electricity
read from it: against the simulated house, whose pump's draw is known."""

import statistics
from datetime import UTC, datetime
from functools import cache, partial
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from plant import begin, draw

from thermaestro.cap.model import Envelope
from thermaestro.core import AuditLog, Values
from thermaestro.core.sensors import SENSORS, SensorHub
from thermaestro.core.values import Key, Sample
from thermaestro.learn import Learnings
from thermaestro.learn import electric as e
from thermaestro.learn.energy import DRAW, PUMP_ENERGY, Energy
from thermaestro.learn.inputs import Context, Recorded, counts_with_compressor, intersect, spans
from thermaestro.learn.tank import Series
from thermaestro.store import Database, Sensor

DAYS = 6
SEED = 9
PER_KWH = 10_000
EVERY_S = 30
STATE = Key("dev", "hp1/compressor.ep14/state")
DEMAND = Key("dev", "hp1/demand")
METER = Key(SENSORS, "meter/energy")


@cache
def recorded(seed: int = SEED, days: int = DAYS) -> tuple[dict[str, list[Sample]], dict[str, Any]]:
    """The plant, its compressor's state and demand a reading a minute, and its own meter
    counting pulses, reported every 30 s in kWh as the sensor turns them; and what the
    pump really drew, by purpose."""
    plant = begin(draw(seed, start=datetime(2026, 1, 12, tzinfo=UTC)))
    out: dict[str, list[Sample]] = {"state": [], "demand": [], "meter": []}
    drawn: dict[str, list[float]] = {"heating": [], "dhw": []}
    pulses = 0.0
    for step in range(days * 24 * 120):
        plant.run(EVERY_S)
        pulses += plant.pump_kw * EVERY_S / 3600 * PER_KWH
        t, on = plant.t, plant.compressor_on
        out["meter"].append(Sample(t, int(pulses) / PER_KWH, None, "good"))
        if step % 2:
            out["state"].append(Sample(t, None, "running" if on else "stopped", "good"))
            out["demand"].append(Sample(t, None, plant.demand, "good"))
        if on and plant.demand in drawn:
            drawn[plant.demand].append(plant.pump_kw)
    return out, {"drawn": drawn, "end": plant.t, "kwh": pulses / PER_KWH}


def serving(series: dict[str, list[Sample]]) -> dict[str, list[tuple[float, float]]]:
    end = series["state"][-1].t
    ran = e.runs(series["state"], end)
    return {
        p: intersect(ran, spans(series["demand"], end, partial(_serves, p)))
        for p in ("heating", "dhw")
    }


def _serves(purpose: str, s: Sample) -> bool:
    return s.text == purpose


def test_a_counter_between_its_readings() -> None:
    counter = Series([Sample(0.0, 1.0, None, "good"), Sample(30.0, 1.3, None, "good")])
    assert e.counter_at(counter, 15.0) == pytest.approx(1.15)  # it rose evenly
    assert e.counter_at(counter, 45.0) == 1.3  # nothing after: the last reading
    restarted = Series(
        [
            Sample(0.0, 5.0, None, "good"),
            Sample(60.0, 5.2, None, "good"),
            Sample(120.0, 0.1, None, "good"),
        ]
    )
    assert e.counter_kwh(restarted, 0.0, 120.0) == pytest.approx(0.3)  # 0.2, then 0.1 anew


def test_the_draw_from_the_pumps_own_meter() -> None:
    series, truth = recorded()
    counter = Series(series["meter"])
    end = series["state"][-1].t
    stood = spans(series["state"], end, lambda s: s.text == "stopped")
    standby = e.standby_kw(stood, counter)
    assert standby == pytest.approx(0.0, abs=0.01)  # the plant draws nothing standing
    runs = e.meter_runs(serving(series), counter, standby or 0.0)
    draws = e.draws(runs)
    for purpose in ("heating", "dhw"):
        assert draws[purpose] == pytest.approx(statistics.median(truth["drawn"][purpose]), rel=0.05)
    # Far more hot water than the house's steps give: charges that start while it heats.
    assert sum(1 for r in runs if r.purpose == "dhw") >= 2 * DAYS


async def test_the_meter_is_checked_against_the_compressor(tmp_path: Path) -> None:
    series, _ = recorded()
    end = series["state"][-1].t
    found = {STATE: series["state"]}

    class Kept(Recorded):
        def __init__(self) -> None:
            self.start, self.end = series["state"][0].t, end

        async def get(self, key: Key) -> list[Sample]:
            return found.get(key, [])

    context = Context(compressor=STATE)
    assert await counts_with_compressor(series["meter"], Kept(), context) is None  # usable
    flat = [Sample(s.t, 12.0, None, "good") for s in series["meter"]]
    judged = await counts_with_compressor(flat, Kept(), context)
    assert judged is not None
    assert judged[0] == "not_usable"


def host() -> Any:
    node = SimpleNamespace
    described = node(
        nodes=[node(path="hp1", kind="unit"), node(path="hp1/compressor.ep14", kind="compressor")],
        points=[node(path="hp1/compressor.ep14/state"), node(path="hp1/demand")],
    )
    return cast(Any, node(instances={"dev": node(described=described)}))


async def test_learned_from_the_meter_with_no_house_meter(tmp_path: Path) -> None:
    series, truth = recorded()
    keys = {"state": STATE, "demand": DEMAND, "meter": METER}
    async with await Database.open(tmp_path / "t.db") as db:
        await db.put(
            Sensor(
                name="Heat pump",
                source="mqtt",
                topic="hhv7/energy/heatpump",
                quantity="energy",
                placement="other",
                per_kwh=PER_KWH,
                meters="dev:hp1",
            ),
            "meter",
        )
        await db.run(
            lambda tr: tr.executemany(
                "INSERT INTO history (instance, point, t, value, text, quality)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                [
                    (k.instance, k.point, s.t, s.value, s.text, s.quality)
                    for name, k in keys.items()
                    for s in series[name]
                ],
            )
        )
        learnings = Learnings(db, AuditLog(tmp_path / "audit"))
        await learnings.load()
        energy = Energy(db, Values(db), host(), learnings, lambda key: True, lambda: None)
        await energy.run(truth["end"])
        found = learnings.get(DRAW, "dev:hp1")
        assert found is not None
        assert isinstance(found.value, dict)
        assert found.value["dhw"] == pytest.approx(
            statistics.median(truth["drawn"]["dhw"]), rel=0.05
        )
        used = learnings.get(PUMP_ENERGY, "dev:hp1")
        assert used is not None
        assert isinstance(used.value, float)
        assert used.value == pytest.approx(truth["kwh"] / DAYS, rel=0.2)


async def test_a_pulse_counter_and_a_home_assistant_entity(tmp_path: Path) -> None:
    async with await Database.open(tmp_path / "t.db") as db:
        values = Values(db)
        now = {"t": 1_800_000_000.0}
        hub = SensorHub(db, values, clock=lambda: now["t"])
        await db.put(
            Sensor(
                name="Pulses",
                source="mqtt",
                topic="hhv7/energy/heatpump",
                quantity="energy",
                placement="other",
                per_kwh=10_000,
            ),
            "pulses",
        )
        await db.put(
            Sensor(
                name="HA",
                source="point",
                point="ha:sensor.heat_pump_energy",
                quantity="energy",
                placement="other",
            ),
            "ha",
        )
        await hub.load()
        hub.receive_mqtt("hhv7/energy/heatpump", b"1234567")
        assert values.latest[Key(SENSORS, "pulses/energy")].value == pytest.approx(123.4567)
        now["t"] += 5
        hub.receive_mqtt("hhv7/energy/heatpump", b"1234600")
        assert values.latest[Key(SENSORS, "pulses/energy")].value == pytest.approx(
            123.4567
        )  # too soon
        now["t"] += 1
        hub.receive_mqtt("hhv7/energy/heatpump", b"12")
        assert values.latest[Key(SENSORS, "pulses/energy")].value == pytest.approx(
            0.0012
        )  # started over
        at = datetime.fromtimestamp(now["t"], UTC)
        values.add(
            "ha",
            Envelope(
                point="sensor.heat_pump_energy",
                value=456789.0,
                unit="Wh",
                t_observed=at,
                t_received=at,
                quality="good",
                source="measured",
            ),
        )
        assert values.latest[Key(SENSORS, "ha/energy")].value == pytest.approx(456.789)


def test_only_an_energy_counter_counts_pulses() -> None:
    with pytest.raises(ValueError, match="only an energy counter"):
        Sensor(name="x", source="mqtt", topic="t", quantity="temperature", per_kwh=1000)
