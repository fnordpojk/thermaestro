"""How far the weather forecasts miss at this house: the bias by lead time, judged before
the planner takes it off, and the spread kept for the optimizer."""

import random
from pathlib import Path

import pytest

from thermaestro.core import AuditLog
from thermaestro.learn import Learned, Learnings
from thermaestro.learn import forecast as f
from thermaestro.store import Database

DAY = 86_400.0
NOW = 1_800_000_000.0


def rows(bias: float, noise: float, days: int = 30, seed: int = 1) -> list[f.Row]:
    """A provider's temperature forecasts at each lead time, every hour, that run `bias`
    warm, give or take `noise`."""
    rng = random.Random(seed)
    out = []
    for hour in range(days * 24):
        valid = NOW - days * DAY + hour * 3600
        observed = 2 + 5 * rng.random()
        for lead in (1, 6, 24, 48):
            out.append(
                f.Row(valid, lead, observed + bias * lead / 48 + rng.gauss(0, noise), observed)
            )
    return out


def test_the_bias_and_spread_by_lead_time() -> None:
    found = rows(bias=1.2, noise=0.3)
    bias = f.biases(found)
    assert bias[48] == pytest.approx(1.2, abs=0.05)
    assert bias[1] == pytest.approx(0.025, abs=0.05)
    spread = f.spreads(found, bias)
    assert spread[48] == pytest.approx(0.3 * 1.645, rel=0.15)  # nine in ten of a normal miss


def test_in_between_lead_times_by_straight_lines() -> None:
    by_lead = {"1": 0.0, "6": 0.5, "24": 1.0, "48": 2.0}
    assert f.at(by_lead, 0.5) == 0.0  # before the first: the first
    assert f.at(by_lead, 15) == pytest.approx(0.75)
    assert f.at(by_lead, 36) == pytest.approx(1.5)
    assert f.at(by_lead, 72) == 2.0  # beyond the last: the last
    assert f.at({}, 12) == 0.0


def test_a_bias_is_proved_only_where_taking_it_off_helps() -> None:
    assert f.judge(rows(bias=2.0, noise=0.3), NOW - 7 * DAY).proved
    assert not f.judge(rows(bias=0.0, noise=0.5), NOW - 7 * DAY).proved  # nothing to take off


async def put(db: Database, found: list[f.Row], source: str = "met") -> None:
    await db.run(
        lambda t: t.executemany(
            "INSERT INTO forecast_leads (source, quantity, valid, lead, value, made, observed)"
            " VALUES (?, 'temperature', ?, ?, ?, ?, ?)",
            [
                (source, r.valid, r.lead, r.value, r.valid - r.lead * 3600, r.observed)
                for r in found
            ],
        )
    )


async def test_learned_from_what_weather_keeps(tmp_path: Path) -> None:
    async with await Database.open(tmp_path / "t.db") as db:
        learnings = Learnings(db, AuditLog(tmp_path / "audit"))
        await learnings.load()
        await put(db, rows(bias=2.0, noise=0.3))
        await put(db, rows(bias=0.0, noise=0.5, seed=2), source="smhi")
        await f.Forecasts(db, learnings).run(NOW)
        met = learnings.get(f.BIAS, "met:temperature")
        assert met is not None
        assert met.state == "proved"
        assert f.correction(learnings, "met", 48) == pytest.approx(2.0, abs=0.1)
        assert f.correction(learnings, "met", 12) == pytest.approx(0.5, abs=0.1)
        smhi = learnings.get(f.BIAS, "smhi:temperature")
        assert smhi is not None
        assert smhi.state == "learning"
        assert f.correction(learnings, "smhi", 48) == 0.0  # not proved: the forecast as given
        assert f.correction(learnings, None, 48) == 0.0
        spread = learnings.get(f.SPREAD, "met:temperature")
        assert spread is not None
        assert spread.kind == "observed"


def test_shown_in_words() -> None:
    from thermaestro.web.learn_operations import _shown

    bias = Learned(
        name=f.BIAS, scope="met:temperature", kind="model", value={"1": 0.1, "48": -1.24}
    )
    assert _shown(bias) == "1 h ahead: +0.1 K, 48 h ahead: -1.2 K"
