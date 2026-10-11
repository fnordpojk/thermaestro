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
from thermaestro.learn import Learned, Learnings
from thermaestro.learn import cop as c
from thermaestro.learn.efficiency import BRINE, DHW, HEATING, Efficiency
from thermaestro.learn.energy import DRAW
from thermaestro.learn.hotwater import HotWater
from thermaestro.store import Database, Rated

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
}


def test_the_documented_figures_give_about_half_the_ideal() -> None:
    f1245 = c.share([(0, 35, 4.47), (0, 45, 3.51)])
    assert f1245 == pytest.approx(0.50, abs=0.01)
    assert c.dhw_share(2.8) == pytest.approx(0.47, abs=0.01)
    assert c.carnot(35, 0) == pytest.approx(308.15 / 35)
    assert c.carnot(20, 19) == pytest.approx(293.15 / c.LIFT_LEAST)  # a small lift, bounded


def _word(value: int) -> float:
    return (value - 0x10000 if value >= 0x8000 else value) / 10


REGISTERS = {"top": 40013, "low": 40014, "brine_in": 40015, "brine_out": 40016, "supply": 40008}


@cache
def recorded(seed: int = SEED, days: int = DAYS) -> tuple[dict[str, list[Sample]], dict[str, Any]]:
    """The plant, a reading a minute, as the pump would show it; and its true COP, the
    share of the ideal it makes heat at, and what the compressor drew, by purpose."""
    plant = begin(draw(seed, start=datetime(2026, 1, 12, tzinfo=UTC)))
    out: dict[str, list[Sample]] = {name: [] for name in POINTS}
    cops: dict[str, list[float]] = {"heating": [], "dhw": []}
    shares: list[float] = []
    drawn: dict[str, list[float]] = {"heating": [], "dhw": []}
    for _ in range(days * 24 * 60):
        plant.run(60)
        on, t = plant.compressor_on, plant.t
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
