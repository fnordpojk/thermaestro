"""The compressor's COP: from its documented figures, and learned from hot-water charges
and the brine side against the simulated house, whose brine flow and COP are known."""

import statistics
from datetime import UTC, datetime
from functools import cache
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from plant import begin, draw

from thermaestro.cap.model import RatedPoint, Rating
from thermaestro.core import AuditLog, Values
from thermaestro.core.values import Sample
from thermaestro.learn import Learned, Learnings, records
from thermaestro.learn import cop as c
from thermaestro.learn.efficiency import BRINE, DHW, HEATING, Efficiency
from thermaestro.learn.energy import DRAW
from thermaestro.learn.hotwater import HotWater
from thermaestro.store import Database, Rated, Sensor

DAYS = 14
SEED = 9
POINTS = {
    "state": "hp1/compressor.ep14/state",
    "demand": "hp1/demand",
    "top": "hp1/dhw/temp.top",
    "low": "hp1/dhw/temp.charge",
    "brine_in": "hp1/brine/brine.in.temp",
    "brine_out": "hp1/brine/brine.out.temp",
    "supply": "hp1/cs1/supply.temp",
    "outdoor": "hp1/outdoor.temp",
}


def test_the_documented_figures_give_about_half_the_ideal() -> None:
    f1245 = c.share([(0, 35, 4.47), (0, 45, 3.51)])
    assert f1245 == pytest.approx(0.50, abs=0.01)
    assert c.dhw_share(2.8) == pytest.approx(0.47, abs=0.01)
    assert c.carnot(35, 0) == pytest.approx(308.15 / 35)
    assert c.carnot(20, 19) == pytest.approx(293.15 / c.LIFT_LEAST)  # a small lift, bounded


def _word(value: int) -> float:
    return (value - 0x10000 if value >= 0x8000 else value) / 10


REGISTERS = {
    "top": 40013,
    "low": 40014,
    "brine_in": 40015,
    "brine_out": 40016,
    "supply": 40008,
    "outdoor": 40004,
}


@cache
def recorded(seed: int = SEED, days: int = DAYS) -> tuple[dict[str, list[Sample]], dict[str, Any]]:
    """The plant, a reading a minute, as the pump would show it; and its true COP, the
    share of the ideal it makes heat at, and what the compressor drew, by purpose."""
    plant = begin(draw(seed, start=datetime(2026, 1, 12, tzinfo=UTC)))
    out: dict[str, list[Sample]] = {name: [] for name in POINTS}
    out["meter"] = []
    kwh = 0.0
    cops: dict[str, list[float]] = {"heating": [], "dhw": []}
    shares: list[float] = []
    drawn: dict[str, list[float]] = {"heating": [], "dhw": []}
    for _ in range(days * 24 * 60):
        plant.run(60)
        on, t = plant.compressor_on, plant.t
        kwh += plant.pump_kw / 60
        out["meter"].append(Sample(t, round(kwh, 4), None, "good"))
        out["state"].append(Sample(t, None, "running" if on else "stopped", "good"))
        out["demand"].append(Sample(t, None, plant.demand, "good"))
        read = {name: _word(plant.registers[r]) for name, r in REGISTERS.items()}
        for name, value in read.items():
            out[name].append(Sample(t, value, None, "good"))
        if on and plant.demand in cops and plant.cop:
            cops[plant.demand].append(plant.cop)
            drawn[plant.demand].append(plant.pump_kw)
            if plant.demand == "heating":
                shares.append(plant.cop / c.carnot(read["supply"], read["brine_in"]))
    return out, {"cop": cops, "share": shares, "drawn": drawn, "end": plant.t}


def host(rating: Rating | None) -> Any:
    node = SimpleNamespace
    described = node(
        nodes=[
            node(path="hp1", kind="unit", rating=None),
            node(path="hp1/dhw", kind="dhw_tank", rating=Rating(liters=180.0, basis="the plant")),
            node(path="hp1/compressor.ep14", kind="compressor", rating=rating),
            node(path="hp1/brine", kind="brine_circuit", rating=None),
            node(path="hp1/cs1", kind="climate_system", rating=None),
        ],
        points=[node(path=p) for p in POINTS.values()],
    )
    return cast(Any, node(instances={"dev": node(described=described)}))


async def learned(tmp_path: Path, rating: Rating | None) -> Learnings:
    series, truth = recorded()
    async with await Database.open(tmp_path / "t.db") as db:
        await db.run(
            lambda tr: tr.executemany(
                "INSERT INTO history (instance, point, t, value, text, quality)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                [
                    ("dev", POINTS[name], s.t, s.value, s.text, s.quality)
                    for name in POINTS
                    for s in series[name]
                ],
            )
        )
        learnings = Learnings(db, AuditLog(tmp_path / "audit"))
        await learnings.load()
        draws = {p: round(statistics.median(v), 3) for p, v in truth["drawn"].items()}
        await learnings.put(
            Learned(name=DRAW, scope="dev:hp1", kind="model", value=draws, state="proved")
        )
        # As in the core: the tank first, so the charges a draw ran into are known.
        await HotWater(db, Values(db), host(rating), learnings, lambda key: True).run(truth["end"])
        efficiency = Efficiency(db, Values(db), host(rating), learnings, lambda key: True)
        await efficiency.run(truth["end"])
        await efficiency.run(truth["end"])  # the brine, proved, lets heating be learned
        return learnings


async def test_metered_the_cop_needs_no_draw(tmp_path: Path) -> None:
    """With the pump's own meter, each charge's electricity is counted, not reckoned."""
    series, truth = recorded()
    async with await Database.open(tmp_path / "t.db") as db:
        await db.put(
            Sensor(
                name="m",
                source="mqtt",
                topic="m",
                quantity="energy",
                placement="other",
                meters="dev:hp1",
            ),
            "meter",
        )
        await db.run(
            lambda tr: tr.executemany(
                "INSERT INTO history (instance, point, t, value, text, quality)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                [
                    ("dev", POINTS[n], s.t, s.value, s.text, s.quality)
                    for n in POINTS
                    for s in series[n]
                ]
                + [
                    ("sensors", "meter/energy", s.t, s.value, None, "good") for s in series["meter"]
                ],
            )
        )
        learnings = Learnings(db, AuditLog(tmp_path / "audit"))
        await learnings.load()
        await HotWater(db, Values(db), host(RATED), learnings, lambda key: True).run(truth["end"])
        await Efficiency(db, Values(db), host(RATED), learnings, lambda key: True).run(truth["end"])
        dhw = learnings.get(DHW, "dev:hp1")
        assert dhw is not None
        assert isinstance(dhw.value, float)
        assert dhw.value * c.carnot(47.5, 5.0) == pytest.approx(
            statistics.median(truth["cop"]["dhw"]), rel=0.25
        )


RATED = Rating(
    heating=(
        RatedPoint(source_c=0, supply_c=35, cop=4.47),
        RatedPoint(source_c=0, supply_c=45, cop=3.51),
    ),
    cop_dhw=2.8,
    basis="the manual",
)


async def test_the_brine_and_the_cop_are_learned_from_the_plant(tmp_path: Path) -> None:
    _, truth = recorded()
    learnings = await learned(tmp_path, RATED)
    brine, dhw, heating = (learnings.get(n, "dev:hp1") for n in (BRINE, DHW, HEATING))
    assert brine is not None
    assert dhw is not None
    assert heating is not None
    assert (brine.state, dhw.state, heating.state) == ("proved", "proved", "proved")
    # The plant's brine: 0.3 l/s, 1200 W/K. The tank's heat is reckoned from the mean of
    # its two sensors, while the plant heats its lower part more: a little short.
    assert brine.value == pytest.approx(1200, rel=0.15)
    # Hot water at a typical charge's 47.5 °C from 5 °C brine, against the plant's own.
    assert isinstance(dhw.value, float)
    assert dhw.value * c.carnot(47.5, 5.0) == pytest.approx(
        statistics.median(truth["cop"]["dhw"]), rel=0.25
    )
    assert heating.value == pytest.approx(statistics.median(truth["share"]), rel=0.2)
    assert dhw.starting == pytest.approx(0.469, abs=0.001)  # Nibe's 2.8, as a share
    assert heating.starting == pytest.approx(0.502, abs=0.001)


async def test_without_documented_figures_it_is_judged_against_what_is_seen(
    tmp_path: Path,
) -> None:
    learnings = await learned(tmp_path, None)
    dhw = learnings.get(DHW, "dev:hp1")
    assert dhw is not None
    assert dhw.starting is None  # nothing documented, nothing entered: no starting value
    assert dhw.value is not None


async def test_without_the_tanks_volume_it_says_so(tmp_path: Path) -> None:
    async with await Database.open(tmp_path / "t.db") as db:
        learnings = Learnings(db, AuditLog(tmp_path / "audit"))
        await learnings.load()
        await learnings.put(
            Learned(name=DRAW, scope="dev:hp1", kind="model", value={"dhw": 1.5}, state="proved")
        )
        found = host(RATED)
        found.instances["dev"].described.nodes[1].rating = None
        efficiency = Efficiency(db, Values(db), found, learnings, lambda key: True)
        await efficiency._judge(next(efficiency.pumps()), Rated(), 2e9, 0.5, 0.47)
        dhw = learnings.get(DHW, "dev:hp1")
        assert dhw is not None
        assert dhw.why == c.NO_VOLUME
        await efficiency._judge(
            next(efficiency.pumps()), Rated(liters={"dev:hp1/dhw": 180.0}), 2e9, 0.5, 0.47
        )
        dhw = learnings.get(DHW, "dev:hp1")
        assert dhw is not None
        assert dhw.why == c.FEW  # the volume entered: now it waits for charges


def test_the_cop_is_shown_in_words() -> None:
    from thermaestro.web.learn_operations import _shown

    heating = Learned(name=HEATING, scope="dev:hp1", kind="model", value=0.5)
    assert _shown(heating) == "COP 4.4 at 0/35 °C, 0.50 of the ideal"
    documented = Learned(name=HEATING, scope="dev:hp1", kind="model", starting=0.5021)
    assert _shown(documented) == "COP 4.4 at 0/35 °C, as documented"
    brine = Learned(name=BRINE, scope="dev:hp1", kind="model", value=1224.0, unit="W/K")
    assert _shown(brine) == "1,224 W per kelvin: about 0.30 l/s of brine"


def air_source(rating: Rating | None) -> Any:
    """The same pump as an air-source one without a tank of its own: no brine, no tank."""
    node = SimpleNamespace
    kept = ("state", "demand", "supply", "outdoor")
    described = node(
        nodes=[
            node(path="hp1", kind="unit", rating=None),
            node(path="hp1/compressor.ep14", kind="compressor", rating=rating),
            node(path="hp1/cs1", kind="climate_system", rating=None),
        ],
        points=[node(path=POINTS[name]) for name in kept],
    )
    return cast(Any, node(instances={"dev": node(described=described)}))


async def test_a_pump_without_brine_or_tank_has_its_hours_read(tmp_path: Path) -> None:
    """The outdoor air is its source; nothing measures its heat, so its heating COP stays as
    documented, and its hours are there for what the radiators give."""
    series, truth = recorded()
    async with await Database.open(tmp_path / "t.db") as db:
        await db.run(
            lambda tr: tr.executemany(
                "INSERT INTO history (instance, point, t, value, text, quality)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                [
                    ("dev", POINTS[name], s.t, s.value, s.text, s.quality)
                    for name in ("state", "demand", "supply", "outdoor")
                    for s in series[name]
                ],
            )
        )
        learnings = Learnings(db, AuditLog(tmp_path / "audit"))
        await learnings.load()
        draws = {p: round(statistics.median(v), 3) for p, v in truth["drawn"].items()}
        await learnings.put(
            Learned(name=DRAW, scope="dev:hp1", kind="model", value=draws, state="proved")
        )
        efficiency = Efficiency(db, Values(db), air_source(RATED), learnings, lambda key: True)
        await efficiency.run(truth["end"])
        hours = [x for _, x in await records.read(db, "dev:hp1", records.COP_HOUR)]
        assert len(hours) > 20
        assert all(x.brine_ks == 0 for x in hours)
        outdoor = statistics.median(s.value for s in series["outdoor"] if s.value is not None)
        assert statistics.median(x.source_c for x in hours) == pytest.approx(outdoor, abs=4)
        heating = learnings.get(HEATING, "dev:hp1")
        assert heating is not None
        assert heating.why == c.NO_SOURCE
        assert heating.used == pytest.approx(0.502, abs=0.001)
        assert learnings.get(DHW, "dev:hp1") is None
        assert learnings.get(BRINE, "dev:hp1") is None


async def test_a_climate_systems_documented_step_reaches_its_curve(tmp_path: Path) -> None:
    from thermaestro.learn.heating import Heating

    node = SimpleNamespace
    step = Rating(offset_step_k=2.5, basis="the manual")
    described = node(
        nodes=[
            node(path="hp1", kind="unit", rating=None),
            node(path="hp1/cs1", kind="climate_system", rating=step),
        ],
        points=[],
        levers=[],
    )
    host = cast(Any, node(instances={"dev": node(described=described)}))
    async with await Database.open(tmp_path / "t.db") as db:
        learnings = Learnings(db, AuditLog(tmp_path / "audit"))
        await learnings.load()
        [keys] = await Heating(db, Values(db), host, learnings, lambda key: True).systems()
        assert keys.step_k == 2.5


def test_a_run_lasts_as_long_as_the_compressor_ran() -> None:
    """The charge sensor shows tank-bottom water for its first minutes, so it isn't good
    then: the water's temperature leaves those minutes out, the run's time doesn't."""
    from thermaestro.learn import tank
    from thermaestro.learn.efficiency import _run

    start = 1_000_000.0
    water = tank.Series(
        [Sample(start, 48.3, None, "transitional"), Sample(start + 480, 50.0, None, "good")]
    )
    inlet = tank.Series([Sample(start - 60, 4.0, None, "good")])
    back = tank.Series([Sample(start - 60, 2.0, None, "good")])
    run = _run(start, [(start, start + 840)], water, inlet, back)
    assert run is not None
    assert run.run_s == 840
    assert run.supply_c == 50.0  # from the good readings only
    assert run.brine_ks == pytest.approx(2.0 * 840)  # the drop over the whole run


async def test_an_entered_brine_flow_gives_the_brine_and_heating(tmp_path: Path) -> None:
    """Hours of heating with the plugin's estimate of the heat from the ground: the brine is
    taken from it, not learned from the charges, and heating's COP comes from it."""
    now = 2_000_000_000.0
    async with await Database.open(tmp_path / "t.db") as db:
        learnings = Learnings(db, AuditLog(tmp_path / "audit"))
        await learnings.load()
        await learnings.put(
            Learned(
                name=DRAW, scope="dev:hp1", kind="model", value={"heating": 1.4}, state="proved"
            )
        )
        found = host(RATED)
        found.instances["dev"].described.points.append(
            SimpleNamespace(path="hp1/brine/heat.extracted.power")
        )
        efficiency = Efficiency(db, Values(db), found, learnings, lambda key: True)
        keys = next(efficiency.pumps())
        assert keys.extracted is not None
        hours = []
        for i in range(c.WARM + c.LEAST + 4):
            # 1.4 kWh an hour, 3.2 K of drop at 1,100 W/K: 3.52 kWh from the ground.
            run = records.CopRun(
                run_s=3600,
                supply_c=38.0,
                source_c=5.0,
                brine_ks=3.2 * 3600,
                kwh=1.4,
                ground_kwh=3.52,
            )
            hours.append(records.row(records.COP_HOUR, now - (i + 1) * 3600, run))
        await db.run(lambda t: records.keep(t, "dev:hp1", hours))
        await efficiency._judge(keys, Rated(), now, 0.5, 0.47)
        brine = learnings.get(BRINE, "dev:hp1")
        assert brine is not None
        assert (brine.kind, brine.why) == ("observed", c.ENTERED)
        assert brine.value == pytest.approx(1100, abs=1)
        heating = learnings.get(HEATING, "dev:hp1")
        assert heating is not None
        assert heating.value == pytest.approx((1.4 + 3.52) / 1.4 / c.carnot(38.0, 5.0), abs=1e-3)


async def test_a_brine_pump_changing_speed_is_followed_hour_by_hour(tmp_path: Path) -> None:
    """In automatic mode the brine pump's speed changes: hours at half the drop and twice
    the flow take as much from the ground. Each hour's own estimate counts, not one figure
    per kelvin for all of them."""
    now = 2_000_000_000.0
    async with await Database.open(tmp_path / "t.db") as db:
        learnings = Learnings(db, AuditLog(tmp_path / "audit"))
        await learnings.load()
        await learnings.put(
            Learned(
                name=DRAW, scope="dev:hp1", kind="model", value={"heating": 1.4}, state="proved"
            )
        )
        found = host(RATED)
        found.instances["dev"].described.points.append(
            SimpleNamespace(path="hp1/brine/heat.extracted.power")
        )
        efficiency = Efficiency(db, Values(db), found, learnings, lambda key: True)
        rows = []
        for i in range(16):
            drop = 3.2 if i % 2 else 1.6  # slow, and twice as fast
            run = records.CopRun(
                run_s=3600,
                supply_c=38.0,
                source_c=5.0,
                brine_ks=drop * 3600,
                kwh=1.4,
                ground_kwh=3.52,
            )
            rows.append(records.row(records.COP_HOUR, now - (i + 1) * 3600, run))
        await db.run(lambda t: records.keep(t, "dev:hp1", rows))
        await efficiency._judge(next(efficiency.pumps()), Rated(), now, 0.5, 0.47)
        heating = learnings.get(HEATING, "dev:hp1")
        assert heating is not None
        assert heating.value == pytest.approx((1.4 + 3.52) / 1.4 / c.carnot(38.0, 5.0), abs=1e-3)


async def test_the_estimate_from_an_entered_flow_is_read_from_the_history(
    tmp_path: Path,
) -> None:
    """The plant's brine carries 1,200 W/K; its plugin, with that flow entered, would show
    the heat taken from the ground as 1.2 kW per kelvin of drop while the compressor runs.
    Read from the history hour by hour, the brine comes out as that, not as the charges have
    it, and every heating hour has its heat from the ground."""
    series, truth = recorded()
    point = "hp1/brine/heat.extracted.power"
    power = []
    for b_in, b_out, state in zip(
        series["brine_in"], series["brine_out"], series["state"], strict=True
    ):
        running = state.text == "running"
        kw = 1.2 * ((b_in.value or 0.0) - (b_out.value or 0.0)) if running else 0.0
        power.append(Sample(b_in.t, round(kw, 3), None, "good"))
    async with await Database.open(tmp_path / "t.db") as db:
        await db.run(
            lambda tr: tr.executemany(
                "INSERT INTO history (instance, point, t, value, text, quality)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                [
                    ("dev", POINTS[name], s.t, s.value, s.text, s.quality)
                    for name in POINTS
                    for s in series[name]
                ]
                + [("dev", point, s.t, s.value, None, "good") for s in power],
            )
        )
        learnings = Learnings(db, AuditLog(tmp_path / "audit"))
        await learnings.load()
        draws = {p: round(statistics.median(v), 3) for p, v in truth["drawn"].items()}
        await learnings.put(
            Learned(name=DRAW, scope="dev:hp1", kind="model", value=draws, state="proved")
        )
        found = host(RATED)
        found.instances["dev"].described.points.append(SimpleNamespace(path=point))
        await HotWater(db, Values(db), found, learnings, lambda key: True).run(truth["end"])
        await Efficiency(db, Values(db), found, learnings, lambda key: True).run(truth["end"])
        hours = [x for _, x in await records.read(db, "dev:hp1", records.COP_HOUR)]
        assert hours
        assert all(x.ground_kwh is not None for x in hours)
        brine = learnings.get(BRINE, "dev:hp1")
        assert brine is not None
        assert brine.why == c.ENTERED
        assert brine.value == pytest.approx(1200, rel=0.05)
        heating = learnings.get(HEATING, "dev:hp1")
        assert heating is not None
        assert heating.value == pytest.approx(statistics.median(truth["share"]), rel=0.2)


def test_the_heat_from_the_ground_is_added_up_minute_by_minute() -> None:
    from thermaestro.learn import tank
    from thermaestro.learn.efficiency import _extracted

    start = 1_000_000.0
    power = tank.Series(
        [Sample(start - 30, 3.0, None, "good"), Sample(start + 600, 4.0, None, "good")]
    )
    found = _extracted(power, [(start, start + 1200)])
    assert found == pytest.approx(3.0 * 10 / 60 + 4.0 * 10 / 60)
    gone = tank.Series([Sample(start - 30, 3.0, None, "good")])
    assert _extracted(gone, [(start, start + 3 * 3600)]) is None  # not known most of it
    backwards = tank.Series(
        [Sample(start - 30, -4.0, None, "good"), Sample(start + 60, 3.0, None, "good")]
    )
    assert _extracted(backwards, [(start, start + 600)]) == pytest.approx(3.0 * 9 / 60)


def test_a_charge_a_draw_ran_into_is_left_out() -> None:
    """A heat pump gives at least the heat of its electricity: a charge whose tank warmed by
    less had hot water drawn while it ran, which the tank's sensors can't tell from it."""
    good = [
        c.Run(float(i), 840.0, 50.0, 4.0, 1500.0, rise_c=3.6, measured_kwh=0.35)
        for i in range(c.LEAST + c.WARM)
    ]
    drawn = [
        c.Run(100.0 + i, 1700.0, 40.0, 5.0, 3900.0, rise_c=2.0, measured_kwh=0.8)
        for i in range(2 * (c.LEAST + c.WARM))
    ]
    alone = c.learn_dhw(good, 178.0, 1.57)
    assert alone is not None
    assert c.learn_dhw(good + drawn, 178.0, 1.57) == alone
    assert c.learn_brine(good + drawn, 178.0, 1.57) == c.learn_brine(good, 178.0, 1.57)
