"""The compressor's electricity learned from the house's meter: against the simulated
house, whose household load comes and goes and whose pump's draw is known, and piece by
piece."""

import statistics
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
from thermaestro.learn import electric as e
from thermaestro.learn.energy import DRAW, STARTS, Energy, charge_kw
from thermaestro.learn.tank import Series
from thermaestro.store import Database

DAYS = 14
SEED = 9
"""A plant whose compressor is small: it draws well under the planner's 2 kW guess."""
STATE = Key("dev", "hp1/compressor.ep14/state")
DEMAND = Key("dev", "hp1/demand")
METER = Key(SITE, "meter/grid.import.power")


@cache
def recorded(seed: int = SEED, days: int = DAYS) -> tuple[dict[str, list[Sample]], dict[str, Any]]:
    """The plant, a reading a minute: the compressor's state, the demand and the house's
    power in W, as a meter would give them; and what the pump really drew."""
    plant = begin(draw(seed, start=datetime(2026, 1, 12, tzinfo=UTC)))
    out: dict[str, list[Sample]] = {"state": [], "demand": [], "meter": []}
    drawn: dict[str, list[float]] = {"heating": [], "dhw": []}
    starts: list[float] = []
    was = False
    for _ in range(days * 24 * 60):
        plant.run(60)
        on = plant.compressor_on
        if on and not was:
            starts.append(plant.t)
        was = on
        out["state"].append(Sample(plant.t, None, "running" if on else "stopped", "good"))
        out["demand"].append(Sample(plant.t, None, plant.demand, "good"))
        out["meter"].append(Sample(plant.t, round(plant.house_kw * 1000), None, "good"))
        if on and plant.demand in drawn:
            drawn[plant.demand].append(plant.pump_kw)
    return out, {"drawn": drawn, "starts": starts, "end": plant.t}


def found_steps() -> list[e.Step]:
    series, _ = recorded()
    end = series["state"][-1].t
    ran = e.runs(series["state"], end)
    return e.steps(ran, Series(series["meter"]), series["demand"], factor=0.001)


def test_the_draw_is_found_through_the_households_load() -> None:
    _, truth = recorded()
    draws = e.draws(found_steps())
    heating = statistics.median(truth["drawn"]["heating"])
    dhw = statistics.median(truth["drawn"]["dhw"])
    assert draws["heating"] == pytest.approx(heating, rel=0.15)
    assert draws["dhw"] == pytest.approx(dhw, rel=0.15)
    assert draws["dhw"] > draws["heating"]  # hot water is made at a higher temperature


def test_the_draw_proves_itself_against_the_guess() -> None:
    _, truth = recorded()
    verdict = e.judge(found_steps(), truth["end"] - e.TEST_S)
    assert verdict.proved, verdict


def test_the_starts_are_counted() -> None:
    series, truth = recorded()
    end = series["state"][-1].t
    ran = e.runs(series["state"], end)
    start = series["state"][0].t + 3600
    assert e.starts(ran, start, end) == sum(1 for t in truth["starts"] if start <= t < end)


def test_an_edge_near_another_isnt_read() -> None:
    meter = Series(
        [
            Sample(float(t), 500.0 + (1500 if 1000 <= t < 1300 else 0), None, "good")
            for t in range(0, 3000, 15)
        ]
    )
    demand = [Sample(0.0, None, "heating", "good")]
    short = e.steps([(1000.0, 1300.0)], meter, demand)
    assert short == []  # its start and stop are too close to tell apart
    meter = Series(
        [
            Sample(float(t), 500.0 + (1500 if 1000 <= t < 2000 else 0), None, "good")
            for t in range(0, 3000, 15)
        ]
    )
    found = e.steps([(1000.0, 2000.0)], meter, demand)
    assert [(s.edge, s.kw) for s in found] == [("start", 1500.0), ("stop", 1500.0)]
    beside = e.steps([(1000.0, 2000.0)], meter, demand, addition=[(900.0, 1100.0)])
    assert [s.edge for s in beside] == ["stop"]  # the addition ran by the start


def node_host() -> Any:
    node = SimpleNamespace
    described = node(
        nodes=[
            node(path="hp1", kind="unit"),
            node(path="hp1/compressor.ep14", kind="compressor"),
        ],
        points=[node(path="hp1/compressor.ep14/state"), node(path="hp1/demand")],
    )
    return cast(Any, node(instances={"dev": node(described=described)}))


async def test_the_compressor_is_learned_day_by_day_and_kept(tmp_path: Path) -> None:
    series, truth = recorded()
    keys = {"state": STATE, "demand": DEMAND, "meter": METER}
    async with await Database.open(tmp_path / "t.db") as db:
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
        energy = Energy(
            db, Values(db), node_host(), learnings, lambda key: True, lambda: (METER, 0.001)
        )
        await energy.run(truth["end"])
        found = learnings.get(DRAW, "dev:hp1")
        assert found is not None
        assert isinstance(found.value, dict)
        assert set(found.value) == {"heating", "dhw"}
        starts = learnings.get(STARTS, "dev:hp1")
        assert starts is not None
        assert isinstance(starts.value, float)
        assert starts.value > 0
        days = await energy._events("dev:hp1", "compressor_day", 0.0)
        # Every whole day: not the first, recorded only from a minute past midnight, nor
        # the last, which isn't over an hour yet.
        assert len(days) == DAYS - 2
        assert all(b["run_s"].get("heating", 0) > 0 for _, b in days)
        await energy.run(truth["end"])  # nothing new to read
        assert len(await energy._events("dev:hp1", "compressor_day", 0.0)) == DAYS - 2
        # Used for a charge only once proved.
        await learnings.put(found.model_copy(update={"state": "learning"}))
        assert charge_kw(learnings) is None
        await learnings.put(found.model_copy(update={"state": "proved"}))
        assert charge_kw(learnings) == found.value["dhw"]


async def test_without_a_house_meter_it_says_why(tmp_path: Path) -> None:
    async with await Database.open(tmp_path / "t.db") as db:
        learnings = Learnings(db, AuditLog(tmp_path / "audit"))
        await learnings.load()
        energy = Energy(db, Values(db), node_host(), learnings, lambda key: True, lambda: None)
        await energy.run(2_000_000_000.0)
        found = learnings.get(DRAW, "dev:hp1")
        assert found is not None
        assert found.args == {"what": "a whole-house power reading"}
        assert found.used is None


def test_the_compressor_is_shown_in_words() -> None:
    from thermaestro.learn import Learned
    from thermaestro.web.learn_operations import _shown

    found = Learned(
        name=DRAW, scope="dev:hp1", kind="model", value={"heating": 1.22, "dhw": 1.5}, unit="kW"
    )
    assert _shown(found) == "1.22 kW heating, 1.50 kW making hot water"
    starts = Learned(name=STARTS, scope="dev:hp1", kind="observed", value=11.4, unit="/day")
    assert _shown(starts) == "11.4 starts a day"
