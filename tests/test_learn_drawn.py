"""What a tank's household will draw by kind of day: the line nine days in ten stay within,
judged against the week and simpler guesses, and planned with once it beats them."""

import itertools
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from thermaestro.core import AuditLog, Values
from thermaestro.core.values import Key
from thermaestro.intents.calendar import Calendar
from thermaestro.learn import Learned, Learnings, drawn, occupied, records
from thermaestro.learn.hotwater import DRAWS, LOSS, HotWater, TankKeys, judged_at, planned
from thermaestro.learn.records import DAY_KIND, TANK_DAY, TankDay
from thermaestro.store import Database

D0 = date(2026, 9, 28)  # a Monday
TANK = "dev:hp1/dhw"


def day(n: int) -> date:
    return D0 + timedelta(days=n)


def shower(size: float, hour: int = 7) -> tuple[float, ...]:
    return tuple(size if h == hour else 0.0 for h in range(24))


def past(n: int, size: float, kind: str = "home", free: bool = False) -> drawn.Past:
    return drawn.Past(day(n), shower(size), kind, free)  # type: ignore[arg-type]


# --- the line ------------------------------------------------------------------------------


def test_the_line_is_what_nine_days_in_ten_stay_within() -> None:
    days = [past(n, float(n + 1)) for n in range(10)]  # showers of 1 to 10
    hours = [(day(20), h) for h in range(24)]
    found = drawn.line(hours, {day(20): drawn.Ahead(day(20), "home", False)}, days, False)
    assert found is not None
    assert found[:2] == [0.0, 0.0]  # nothing before 07:00
    assert found[2] == 9.0  # by 09:00: nine of the ten stay within 9
    assert found[-1] == 9.0
    assert sum(drawn.hourly(found)) == pytest.approx(9.0)
    assert all(a <= b for a, b in itertools.pairwise(found))
    assert min(drawn.hourly(found)) >= 0.0


def test_two_days_ahead_are_any_two_past_days() -> None:
    days = [past(n, 2.0) for n in range(5)] + [past(5, 10.0)]
    hours = [(day(20), h) for h in range(24)] + [(day(21), h) for h in range(24)]
    ahead = {d: drawn.Ahead(d, "home", False) for d in (day(20), day(21))}
    found = drawn.line(hours, ahead, days, False)
    assert found is not None
    # 36 combinations: 25 of 2 + 2, 10 with one 10, one of 10 + 10; nine in ten within 12.
    assert found[-1] == 12.0
    seasoned = {d: drawn.Ahead(d, "home", False, 1.5) for d in (day(20), day(21))}
    assert drawn.line(hours, seasoned, days, False)[-1] == pytest.approx(18.0)  # type: ignore[index]


def test_a_kind_seen_too_little_takes_more_days() -> None:
    days = [past(n, 5.0) for n in range(6)] + [past(6, 0.0, "away"), past(7, 0.0, "away")]
    assert len(drawn.pool(days, "away", False, False)) == 8  # two days away: every day
    days.append(past(8, 0.0, "away"))
    assert {d.kind for d in drawn.pool(days, "away", False, False)} == {"away"}
    days += [past(9, 9.0, free=True), past(10, 9.0, free=True)]
    assert len(drawn.pool(days, "home", True, True)) == 8  # two free days: all home ones
    days.append(past(11, 9.0, free=True))
    assert {d.free for d in drawn.pool(days, "home", True, True)} == {True}
    assert drawn.pool([past(0, 1.0, "unknown")], "home", False, False) == []


def test_each_day_ahead_takes_its_kind() -> None:
    """Away while a request holds, a return the day it ends, home after; else as last
    concluded."""
    back = datetime.combine(day(1), datetime.min.time(), UTC) + timedelta(hours=12)

    def away(at: datetime) -> bool:
        return at < back

    days = [day(0), day(1), day(2)]
    kinds = drawn.kinds_ahead(days, UTC, "home", away, lambda d: d.weekday() >= 5)
    assert [kinds[d][0] for d in days] == ["away", "return", "home"]
    leaving = drawn.kinds_ahead(days, UTC, "home", lambda at: at >= back, lambda d: False)
    assert [leaving[d][0] for d in days] == ["home", "departure", "away"]
    nobody = drawn.kinds_ahead(days, UTC, "away", lambda at: False, lambda d: False)
    assert {k for k, _ in nobody.values()} == {"away"}
    assert (
        drawn.kinds_ahead(days, UTC, None, lambda at: False, lambda d: False)[day(0)][0] == "home"
    )


# --- judging it ----------------------------------------------------------------------------


def moment(actual: list[float], mine: list[float], other: list[float]) -> drawn.Moment:
    guesses = {g: other for g in drawn.OTHERS} | {"plain": mine, "by_class": mine}
    return drawn.Moment(tuple(actual), "home", guesses)


def test_it_is_judged_by_its_quantile_loss_against_the_others() -> None:
    near = [moment([5.0] * 16, [5.5] * 16, [3.0] * 16) for _ in range(7)]
    found = drawn.score(near)
    assert found.proved
    assert found.why == drawn.PROVED
    assert (found.args["within"], found.args["blocks"]) == (7 * 16, 7 * 16)
    assert found.args["within_home"] == 1.0
    assert found.args["days_home"] == 7
    # A line far too high: always within, but it costs more than a guess a little low.
    high = [moment([5.0] * 16, [50.0] * 16, [4.0] * 16) for _ in range(7)]
    assert not drawn.score(high).proved
    assert drawn.score(near[:6]).why == drawn.FEW


def test_the_quantile_loss_weighs_too_low_nine_times_too_high() -> None:
    assert drawn.pinball(10.0, 9.0) == pytest.approx(0.9)
    assert drawn.pinball(9.0, 10.0) == pytest.approx(0.1)


# --- the learner and the planner -------------------------------------------------------------


@pytest.fixture
async def db(tmp_path: Path) -> AsyncIterator[Database]:
    async with await Database.open(tmp_path / "t.db") as db:
        yield db


TRIPS = (range(20, 24), range(33, 37))
DAYS = 40


def drawn_on(n: int) -> tuple[float, ...]:
    """A shower at 07:00 and the dishes at 19:00 at home, a little more on some days;
    nothing while away."""
    if any(n in t for t in TRIPS):
        return (0.0,) * 24
    out = [0.0] * 24
    out[7] = 6.0 + (n % 3)
    out[19] = 3.0
    return tuple(out)


async def recorded(db: Database) -> None:
    week = [0.0] * 24
    week[7], week[19] = 6.0, 3.0
    rows = [
        records.row(
            TANK_DAY,
            datetime.combine(day(n), datetime.min.time(), UTC).timestamp(),
            TankDay(
                day=day(n),
                hourly=list(drawn_on(n)),
                predicted=week,  # the week: a day at home, every day
                baseline=[9.0 / 24] * 24,
                draws=2,
                drawn=9.0,
                charges=1,
                k=0.01,
                opens=0.5,
                season=1.0,
            ),
        )
        for n in range(DAYS)
    ]
    await db.run(lambda t: records.keep(t, TANK, rows))
    conclusions = [(datetime.combine(day(0), datetime.min.time(), UTC).timestamp(), "home")]
    for trip in TRIPS:
        # Gone at 20:00 the evening before, back at 18:00 on the trip's last day.
        left = datetime.combine(day(trip[0]), datetime.min.time(), UTC) - timedelta(hours=4)
        back = datetime.combine(day(trip[-1]), datetime.min.time(), UTC) + timedelta(hours=18)
        conclusions += [(left.timestamp(), "away"), (back.timestamp(), "home")]
    await db.run(
        lambda t: t.executemany(
            "INSERT INTO occupancy (concluded, began, state, why, args)"
            " VALUES (?, ?, ?, 'x', '{}')",
            [(at, at, state) for at, state in conclusions],
        )
    )


async def learned(db: Database, tmp_path: Path) -> tuple[Learnings, float]:
    learnings = Learnings(db, AuditLog(tmp_path / "audit"))
    await learnings.load()
    tank = HotWater(
        db, Values(db), cast(Any, SimpleNamespace(instances={})), learnings, lambda k: True
    )
    now = datetime.combine(day(DAYS), datetime.min.time(), UTC).timestamp() + 3600
    point = Key("dev", "hp1/dhw/temp.top")
    keys = TankKeys(TANK, point, point, point, point)
    await tank._by_kind(keys, Calendar(UTC), now)
    return learnings, now


async def test_the_learner_proves_it_where_the_household_comes_and_goes(
    db: Database, tmp_path: Path
) -> None:
    await recorded(db)
    learnings, _ = await learned(db, tmp_path)
    found = learnings.get(DRAWS, TANK)
    assert found is not None
    assert (found.state, found.why) == ("proved", drawn.PROVED), found.args
    assert int(found.args["days_away"]) >= 2
    kinds = {k.day: k.kind for _, k in await records.read(db, TANK, DAY_KIND)}
    assert [kinds[day(n)] for n in (19, 20, 22, 23, 24)] == [
        "departure",
        "away",
        "away",
        "return",
        "home",
    ]


def test_a_midnight_is_judged_with_what_was_concluded_by_then() -> None:
    """Away for three days, nothing drawn; later, nobody home concluded at 10:00 from twelve
    quiet hours, dated from 22:00 the evening before: at that midnight it wasn't known, so
    the line was two days at home's, not two days away's."""
    away = (1, 2, 3, 7, 8, 9)
    read = {day(n): (0.0,) * 24 if n in away else drawn_on(n) for n in range(10)}
    predicted: dict[date, tuple[list[float] | None, list[float] | None]] = {
        d: (None, None) for d in read
    }

    def at(n: int, hour: float = 0.0) -> float:
        return datetime.combine(day(n), datetime.min.time(), UTC).timestamp() + hour * 3600

    rows: list[occupied.Row] = [
        (at(0), at(0), "home"),
        (at(1), at(1), "away"),  # the alarm, at once
        (at(4), at(4), "home"),
        (at(7, 10), at(6, 22), "away"),
    ]
    found = judged_at(day(7), "away", read, predicted, rows, UTC, lambda d: False)
    assert found.kind == "away"
    assert found.guesses["plain"][-1] >= 2 * 9.0
    later = judged_at(day(8), "away", read, predicted, rows, UTC, lambda d: False)
    assert later.guesses["plain"][-1] == 0.0  # by then it was known: two days away


async def test_the_plan_gets_the_line_for_who_is_home(db: Database, tmp_path: Path) -> None:
    await recorded(db)
    learnings, now = await learned(db, tmp_path)
    await learnings.put(Learned(name=LOSS, scope=TANK, kind="model", value=0.01, state="proved"))
    start = datetime.fromtimestamp(now, UTC)
    calendar = Calendar(UTC)
    home = await planned(db, learnings, TANK, calendar, lambda d: 1.0, start, "home")
    away = await planned(db, learnings, TANK, calendar, lambda d: 1.0, start, "away")
    assert home is not None
    assert away is not None
    assert home[0] == pytest.approx(0.01)
    assert len(home[1]) == drawn.HORIZON_H
    assert sum(home[1]) >= 2 * 9.0  # two days at home, the heavier ones
    assert sum(away[1]) == pytest.approx(0.0)


async def test_until_proved_the_plan_keeps_the_week(db: Database, tmp_path: Path) -> None:
    learnings = Learnings(db, AuditLog(tmp_path / "audit"))
    await learnings.load()
    start = datetime(2026, 11, 9, tzinfo=UTC)
    assert await planned(db, learnings, TANK, Calendar(UTC), lambda d: 1.0, start) is None
