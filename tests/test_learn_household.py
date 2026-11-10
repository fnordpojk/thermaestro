"""The house with the household's heat: learned beside the model as it is, from the hours
whose occupancy is known, and planned with only once it has beaten it."""

import math
import random
from collections.abc import AsyncIterator, Callable
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pytest

from thermaestro.core import AuditLog, Values
from thermaestro.core.values import Key
from thermaestro.intents import kinds
from thermaestro.intents.resolve import Resolver
from thermaestro.learn import Learned, Learnings, records
from thermaestro.learn import house as h
from thermaestro.learn.heating import HOUSEHOLD, MODEL, Heating, SystemKeys
from thermaestro.learn.records import HouseDay, HouseHour
from thermaestro.planner.optimizer import HORIZON_H, Optimizer
from thermaestro.store import Database, Home

START = datetime(2026, 1, 12, tzinfo=UTC).timestamp()
SCOPE = "dev:hp1/cs1"


def at_work(k: int) -> bool:
    """Everyone out from 08:00 to 16:00, every day."""
    return 8 <= k % 24 < 16


def household(
    hours: int = 240,
    eta: float = 0.4,
    away: Callable[[int], bool] = at_work,
    seed: int = 1,
) -> list[h.Hour]:
    """A house that is the model itself, with a household keeping it `eta / 0.4` °C warmer
    (alpha 0.3, beta 0.1) while home; the water varied by hand, as price shifting would."""
    rng = random.Random(seed)
    truth = h.Fit(0.3, 0.1, 0.02, 0.0, 0.5, 0.25, 3, 0, 0.0, eta, True)
    out: list[h.Hour] = []
    mass = room = 21.0
    for k in range(hours):
        outdoor = 2 + 5 * math.sin(k / 30) + rng.uniform(-1, 1)
        water = 32 + 6 * math.sin(k / 7) + rng.uniform(-3, 3)
        sun = max(0.0, 300 * math.sin(math.pi * ((k % 24) - 8) / 8)) if 8 <= k % 24 <= 16 else 0
        home = 0.0 if away(k) else 1.0
        hour = h.Hour(START + k * 3600, 0.0, outdoor, water, sun, 3.0, home=home)
        out.append(hour)
        mean = h.lagged(out, k, 3) or water
        if k:
            mass += truth.r * (room - mass)
        room = truth.room(mass, hour, mean)
        out[k] = h.Hour(hour.t, round(room, 3), outdoor, water, sun, 3.0, home=home)
    return out


# --- learning it ------------------------------------------------------------------------


def test_the_households_heat_is_found_again() -> None:
    fit, why, _ = h.fit(h.labeled(household()), household=True)
    assert why is None
    assert fit is not None
    assert fit.household
    assert fit.settled == pytest.approx(1.0, rel=0.2)
    assert fit.as_value()["eta"] == pytest.approx(fit.eta, abs=1e-5)
    assert "eta" not in h.fit(household())[0].as_value()  # type: ignore[union-attr]


def test_with_someone_always_home_it_isnt_learned() -> None:
    """It can't be told from the rest: nothing is claimed."""
    fit, why, args = h.fit(h.labeled(household(away=lambda k: False)), household=True)
    assert (fit, why) == (None, h.NO_AWAY)
    assert args == {"hours": 0, "least": h.AWAY_LEAST}


def test_hours_whose_occupancy_isnt_known_are_left_out() -> None:
    hours = household()
    hours[50] = h.Hour(hours[50].t, hours[50].room, hours[50].outdoor, hours[50].water)
    known = h.labeled(hours)
    assert known[50].skip == "unknown"
    assert [x.skip for i, x in enumerate(known) if i != 50] == [None] * (len(hours) - 1)
    assert h.simulate(known, 46, h.Fit(0.3, 0.1, 0.0, 0.0, 0.0, 0.25, 3, 0, 0.0)) is None


# --- judging it against the model as it is ----------------------------------------------


def test_it_beats_the_model_as_it_is_where_the_household_comes_and_goes() -> None:
    hours = household()
    (current, before, _, _), (found, verdict, _, _) = h.contest(hours, hours[-48].t)
    assert current is not None
    assert before is not None
    assert found is not None
    assert verdict is not None
    assert verdict.proved, verdict
    assert verdict.why == h.OVER
    args = verdict.args
    assert float(args["away"]) < float(args["away_current"])
    assert float(args["error"]) <= 0.9 * float(args["current"])
    assert float(args["settled"]) == pytest.approx(1.0, rel=0.2)


def test_tested_only_while_away_says_so() -> None:
    """Out at work at first, then away for the two days tested: no figures at home are
    given, as there were none."""
    hours = household(away=lambda k: k >= 192 or at_work(k))
    _, (_, verdict, _, _) = h.contest(hours, hours[-48].t)
    assert verdict is not None
    assert verdict.why in (h.OVER_AWAY, h.NOT_OVER_AWAY)
    assert "home" not in verdict.args


def test_a_household_that_adds_nothing_doesnt_replace_the_model() -> None:
    """Out every day, but their heat makes no difference: the term finds next to nothing,
    and isn't better than the model as it is by the margin."""
    hours = household(eta=0.0)
    _, (_, verdict, _, _) = h.contest(hours, hours[-48].t)
    assert verdict is not None
    assert not verdict.proved
    assert verdict.why == h.NOT_OVER


def test_without_time_away_in_the_hours_tested_it_isnt_judged() -> None:
    """Out only in the first days: learned, but not judged until a test has time away."""
    hours = household(away=lambda k: k < 120 and at_work(k))
    _, (found, verdict, _, _) = h.contest(hours, hours[-48].t)
    assert found is not None
    assert verdict is not None
    assert (verdict.proved, verdict.why) == (False, h.FEW_AWAY)


# --- the learner ------------------------------------------------------------------------


@pytest.fixture
async def db(tmp_path: Path) -> AsyncIterator[Database]:
    async with await Database.open(tmp_path / "t.db") as db:
        yield db


async def kept(db: Database, hours: list[h.Hour]) -> None:
    """The hours as the learner keeps them, a day a row, without who was home."""
    days: dict[date, list[HouseHour]] = {}
    for x in hours:
        day = datetime.fromtimestamp(x.t, UTC).date()
        days.setdefault(day, []).append(
            HouseHour(
                t=x.t,
                room=x.room,
                outdoor=x.outdoor,
                water=x.water,
                sun=x.sun,
                wind=x.wind,
                supply=x.water,
                back=x.water,
            )
        )
    rows = [
        records.row(
            records.HOUSE_DAY,
            datetime.combine(day, datetime.min.time(), UTC).timestamp(),
            HouseDay(day=day, hours=found),
        )
        for day, found in days.items()
    ]
    await db.run(lambda t: records.keep(t, SCOPE, rows))


async def concluded(db: Database, hours: list[h.Hour]) -> None:
    """What occupancy concluded: home and away as the household came and went."""
    rows: list[tuple[float, float, str, str]] = []
    state = None
    for x in hours:
        now = "home" if x.home else "away"
        if now != state:
            rows.append((x.t, x.t, now, "alarm" if now == "away" else "disarmed"))
            state = now
    await db.run(
        lambda t: t.executemany(
            "INSERT INTO occupancy (concluded, began, state, why, args) VALUES (?, ?, ?, ?, '{}')",
            rows,
        )
    )


async def judged(db: Database, tmp_path: Path, now: float) -> Learnings:
    learnings = Learnings(db, AuditLog(tmp_path / "audit"))
    await learnings.load()
    point = Key("dev", "hp1/cs1/supply.temp")
    keys = SystemKeys(SCOPE, point, point, [], point, point, [])
    learner = Heating(db, Values(db), cast(Any, None), learnings, lambda key: True)
    await learner._judge(keys, Home(), now, True, UTC)
    return learnings


async def test_the_learner_keeps_the_households_model_beside_the_other(
    db: Database, tmp_path: Path
) -> None:
    """From who was home as occupancy concluded it, each hour: proved by beating the model
    as it is, which stays as it was learned."""
    hours = household(hours=10 * 24)
    await kept(db, hours)
    await concluded(db, hours)
    learnings = await judged(db, tmp_path, hours[-1].t + 3600)
    found = learnings.get(HOUSEHOLD, SCOPE)
    assert found is not None
    assert (found.state, found.why) == ("proved", h.OVER), found.args
    assert isinstance(found.value, dict)
    assert "eta" in found.value
    model = learnings.get(MODEL, SCOPE)
    assert model is not None
    assert isinstance(model.value, dict)
    assert "eta" not in model.value


async def test_without_occupancy_the_learner_learns_no_household(
    db: Database, tmp_path: Path
) -> None:
    """Nothing concluded and no draws kept: no hour is known, so nothing is claimed."""
    hours = household(hours=10 * 24)
    await kept(db, hours)
    learnings = await judged(db, tmp_path, hours[-1].t + 3600)
    found = learnings.get(HOUSEHOLD, SCOPE)
    assert found is not None
    assert (found.state, found.value, found.why) == ("learning", None, h.NO_AWAY)


# --- the replay's report --------------------------------------------------------------------


async def test_the_replay_reports_the_house_by_whether_anyone_was_home(
    db: Database, tmp_path: Path
) -> None:
    """Each day judged with the models as they stood at its start: away, the household's
    heat follows the rooms where the model without it can't."""
    from types import SimpleNamespace

    from thermaestro.replay.conditions import Conditions

    hours = household(hours=6 * 24)
    await kept(db, hours)
    await concluded(db, hours)
    point = Key("dev", "hp1/cs1/supply.temp")
    keys = SystemKeys(SCOPE, point, point, [], point, point, [])

    async def systems() -> list[SystemKeys]:
        return [keys]

    learning = SimpleNamespace(
        heating=SimpleNamespace(systems=systems),
        hot_water=SimpleNamespace(tanks=list),
        usable=lambda key: True,
    )
    judged = Conditions(db, cast(Any, learning), cast(Any, None), cast(Any, None))
    as_is = h.Fit(0.3, 0.1, 0.02, 0.0, 0.7, 0.25, 3, 0, 0.0)
    theirs = h.Fit(0.3, 0.1, 0.02, 0.0, 0.5, 0.25, 3, 0, 0.0, 0.4, True)
    days = sorted({datetime.fromtimestamp(x.t, UTC).date() for x in hours})[1:]
    for d in days:
        judged._house[d] = {SCOPE: (as_is, theirs)}
    found = await judged._houses(await occupied_timeline(db, hours), days, hours[0].t)
    away, home = found[SCOPE]["away"], found[SCOPE]["home"]
    assert away["both"] == away["hours"] > 0
    assert away["household"] < 0.01  # the house itself
    assert away["model_both"] > 0.1
    assert home["hours"] > 0


async def occupied_timeline(db: Database, hours: list[h.Hour]) -> Any:
    from thermaestro.learn import occupied

    return await occupied.timeline(db, hours[-1].t + 3600, hours[0].t, [], UTC)


# --- planning with it -------------------------------------------------------------------


def optimizer(learnings: Learnings, occupied: str | None) -> Optimizer:
    opt = Optimizer(cast(Any, None), cast(Any, None), cast(Any, None), learnings)
    opt.occupied = lambda: occupied
    return opt


async def test_the_plan_expects_who_is_home_as_last_concluded(db: Database, tmp_path: Path) -> None:
    learnings = Learnings(db, AuditLog(tmp_path / "audit"))
    starts = [START + 900 * j for j in range(4 * HORIZON_H)]
    nobody = Resolver([], {})
    assert optimizer(learnings, "away")._home_ahead(nobody, starts) == (0.0,) * HORIZON_H
    assert optimizer(learnings, "home")._home_ahead(nobody, starts) == (1.0,) * HORIZON_H
    assert optimizer(learnings, None)._home_ahead(nobody, starts) == (1.0,) * HORIZON_H


async def test_an_away_request_holds_until_its_end_then_home(db: Database, tmp_path: Path) -> None:
    learnings = Learnings(db, AuditLog(tmp_path / "audit"))
    created = datetime.fromtimestamp(START - 3600, UTC)
    back = created + timedelta(hours=11)  # ten hours into the plan
    request = kinds.away(back, {}, principal="u:1", created=created)
    resolver = Resolver([request], {})
    starts = [START + 900 * j for j in range(4 * HORIZON_H)]
    ahead = optimizer(learnings, "away")._home_ahead(resolver, starts)
    assert ahead == (0.0,) * 10 + (1.0,) * (HORIZON_H - 10)


async def test_the_household_model_is_planned_with_once_proved(
    db: Database, tmp_path: Path
) -> None:
    """Learned but not proved, the model as it is is planned with; proved, it is."""
    learnings = Learnings(db, AuditLog(tmp_path / "audit"))
    as_is = h.Fit(0.3, 0.1, 0.02, 0.0, 0.5, 0.25, 3, 0, 0.1).as_value()
    theirs = h.Fit(0.3, 0.1, 0.02, 0.0, 0.1, 0.25, 3, 0, 0.1, 0.4, True).as_value()
    await learnings.put(Learned(name=MODEL, scope=SCOPE, kind="model", value=as_is, state="proved"))
    await learnings.put(
        Learned(name=HOUSEHOLD, scope=SCOPE, kind="model", value=theirs, state="learning")
    )
    opt = optimizer(learnings, "home")
    assert opt.house_model(SCOPE) == as_is
    await learnings.put(
        Learned(name=HOUSEHOLD, scope=SCOPE, kind="model", value=theirs, state="proved")
    )
    assert opt.house_model(SCOPE) == theirs
    assert h.Fit.from_value(theirs).household
    assert not h.Fit.from_value(as_is).household
