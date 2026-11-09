"""Whether anyone was home, hour by hour and day by day, as the learners read it: from what
occupancy concluded, and before it, from the draws the tank's learner kept."""

from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from thermaestro.learn import records
from thermaestro.learn.occupied import Row, Timeline, line, timeline
from thermaestro.learn.records import DRAW, TANK_DAY, DrawSeen, Gap, TankDay
from thermaestro.occupancy import State
from thermaestro.store import Database

TANK = "pump:hp1/dhw"
D0 = date(2026, 10, 5)


def at(day: int, hour: float) -> float:
    """`hour` o'clock on day `day` from `D0`, UTC."""
    start = datetime.combine(D0 + timedelta(days=day), datetime.min.time(), UTC)
    return (start + timedelta(hours=hour)).timestamp()


def on(day: int) -> date:
    return D0 + timedelta(days=day)


@pytest.fixture
async def db(tmp_path: Path) -> AsyncIterator[Database]:
    async with await Database.open(tmp_path / "t.db") as db:
        yield db


async def concluded(db: Database, *rows: tuple[float, float | None, State, str]) -> None:
    await db.run(
        lambda t: t.executemany(
            "INSERT INTO occupancy (concluded, began, state, why, args) VALUES (?, ?, ?, ?, '{}')",
            rows,
        )
    )


async def kept(
    db: Database, read: list[int], draws: list[float], gaps: tuple[int, ...] = ()
) -> None:
    """The tank's learner's days read, as kept: `read` days, the draws in them, and gaps."""
    days = [
        records.row(
            TANK_DAY,
            at(n, 0),
            TankDay(
                day=on(n),
                hourly=[0.0] * 24,
                predicted=None,
                baseline=None,
                draws=0,
                drawn=0.0,
                charges=0,
                k=0.0,
                opens=0.5,
                season=1.0,
            ),
        )
        for n in read
    ]
    days += [records.row(TANK_DAY, at(n, 0), Gap(day=on(n), skipped="gap")) for n in gaps]
    seen = [records.row(DRAW, d, DrawSeen(start=d, end=d + 300, size=4.0)) for d in draws]
    await db.run(lambda t: records.keep(t, TANK, [*days, *seen]))


# --- from what occupancy concluded ---------------------------------------------------------------


async def test_an_absence_counts_from_its_start_once_it_is_concluded(db: Database) -> None:
    """Twelve quiet hours conclude an absence dated from the last activity: read before it
    was concluded, those hours were home, and nothing concluded later is used."""
    await concluded(
        db,
        (at(0, 8.5), at(0, 8), "home", "activity"),
        (at(1, 7), at(0, 19), "away", "quiet"),
    )
    before = await timeline(db, at(0, 23), at(0, 0), [], UTC)
    assert before.at(at(0, 7)) == "unknown"
    assert before.at(at(0, 21)) == "home"
    after = await timeline(db, at(1, 8), at(0, 0), [], UTC)
    assert after.at(at(0, 18)) == "home"
    assert after.at(at(0, 21)) == "away"


def test_a_later_conclusion_overrides_from_when_it_began() -> None:
    rows: list[Row] = [
        (at(0, 8), at(0, 8), "home"),
        (at(2, 8), at(1, 12), "away"),  # put right later: away since the day before
        (at(3, 9), at(3, 9), "home"),
    ]
    seen = line(rows)
    assert [seen.at(at(1, h)) for h in (11, 13)] == ["home", "away"]
    assert seen.at(at(3, 10)) == "home"
    assert seen.starts == (at(0, 8), at(1, 12), at(3, 9))


def test_an_hour_is_what_held_through_most_of_it() -> None:
    seen = Timeline((at(0, 8.25), at(0, 9.75)), ("home", "away"))
    assert seen.hour(at(0, 8)) == "home"  # three quarters home
    assert seen.hour(at(0, 9)) == "home"
    assert seen.hour(at(0, 7)) == "unknown"
    half = Timeline((at(0, 8), at(0, 8.5)), ("home", "away"))
    assert half.hour(at(0, 8)) == "home"  # half each: the earlier


# --- days ----------------------------------------------------------------------------------------


def test_days_are_labeled_by_their_waking_hours() -> None:
    seen = line(
        [
            (at(0, 6), at(0, 6), "home"),
            (at(1, 12), at(1, 12), "away"),  # leaves at noon
            (at(3, 18), at(3, 18), "home"),  # back in the evening
            (at(4, 8), at(4, 8), "away"),  # at work and at school
            (at(4, 16), at(4, 16), "home"),
            (at(5, 23), at(5, 23), "away"),  # leaves at night
            (at(7, 12), at(7, 12), "home"),  # the cat sitter, for an hour
            (at(7, 13), at(7, 13), "away"),
        ]
    )
    days = [seen.day(on(n), UTC) for n in range(9)]
    assert days == [
        "home",
        "departure",
        "away",
        "return",
        "home",  # out between, home at both ends
        "home",
        "away",
        "away",  # a visit leaves the day away
        "away",
    ]
    assert seen.day(on(-1), UTC) == "unknown"


def test_a_day_with_a_waking_hour_not_known_is_not_known() -> None:
    seen = line([(at(0, 12), at(0, 12), "home")])
    assert seen.day(on(0), UTC) == "unknown"
    assert seen.day(on(1), UTC) == "home"


# --- before occupancy was weighed live -------------------------------------------------------


async def test_the_kept_draws_are_weighed_as_a_house_with_one_source(db: Database) -> None:
    """Home from a draw; away once the tank was read and quiet for a day, from the last draw;
    home again at the next."""
    draws = [at(0, 7), at(0, 19), at(1, 8), at(4, 8), at(4, 20), at(5, 8), at(5, 20)]
    await kept(db, list(range(6)), draws)
    seen = await timeline(db, at(6, 1), at(0, 0), [TANK], UTC)
    assert seen.at(at(0, 6)) == "unknown"
    assert seen.at(at(0, 7.5)) == "home"
    assert seen.at(at(1, 8.5)) == "away"  # concluded a day on, from the last draw
    assert seen.at(at(4, 8.5)) == "home"
    assert [seen.day(on(n), UTC) for n in range(6)] == [
        "home",
        "departure",
        "away",
        "away",
        "return",
        "home",
    ]


async def test_a_day_the_tank_wasnt_read_isnt_quiet(db: Database) -> None:
    await kept(db, [0, 2], [at(0, 8), at(2, 20)], gaps=(1,))
    seen = await timeline(db, at(3, 1), at(0, 0), [TANK], UTC)
    assert "away" not in seen.states  # quiet from 08:00, but not for a day with the tank read


@dataclass
class Bound:
    value: object


class Force:
    def __init__(self, at: datetime) -> None:
        self._at = at

    def bound(self, target: str, scope: str) -> Bound | None:
        night = self._at.hour >= 22 or self._at.hour < 7
        return Bound(True) if target == "quiet" and scope == "house" and night else None


class Quiet:
    """Quiet hours from 22:00 to 07:00 UTC, as the household's intents give them."""

    async def resolver(self) -> "Quiet":
        return self

    def in_force(self, at: datetime) -> Force:
        return Force(at)


async def test_the_households_quiet_hours_are_not_counted(db: Database) -> None:
    """From a draw at 18:00: four hours to 22:00 and fifteen the next day count, nineteen;
    without quiet hours, a day."""
    await kept(db, [0, 1, 2], [at(0, 18)])
    asleep = await timeline(db, at(2, 6), at(0, 0), [TANK], UTC, Quiet())  # type: ignore[arg-type]
    assert "away" not in asleep.states
    awake = await timeline(db, at(2, 6), at(0, 0), [TANK], UTC)
    assert awake.at(at(0, 19)) == "away"


async def test_what_occupancy_concluded_takes_over_from_the_kept_draws(db: Database) -> None:
    """The draws are weighed only until occupancy's first conclusion; from then on, only
    what it concluded counts."""
    await kept(db, list(range(5)), [at(0, 8), at(4, 12)])
    await concluded(
        db,
        (at(3, 10), at(3, 10), "home", "disarmed"),
        (at(4, 8), at(4, 8), "away", "alarm"),
    )
    seen = await timeline(db, at(5, 0), at(0, 0), [TANK], UTC)
    assert seen.at(at(2, 0)) == "away"  # from the draws: a day quiet after 08:00
    assert seen.at(at(3, 11)) == "home"
    assert seen.at(at(4, 13)) == "away"  # the alarm, whatever the draw at noon
