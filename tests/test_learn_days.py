"""Which days a learner reads, and whether a day's readings cover it."""

from datetime import UTC, date, datetime

from thermaestro.core.values import Sample
from thermaestro.learn import tank
from thermaestro.learn.heating import _hour

DAY = date(2026, 10, 15)
T0 = datetime(2026, 10, 15, tzinfo=UTC).timestamp()


def every(seconds: float, start: float, end: float) -> list[Sample]:
    n = int((end - start) // seconds) + 1
    return [Sample(start + i * seconds, 1.0, None, "good") for i in range(n)]


def test_a_day_is_covered_only_when_its_readings_go_on_through_it() -> None:
    whole = every(300, T0 - 600, T0 + 86_400)
    assert tank.covers(whole, T0, T0 + 86_400)
    hole = [s for s in whole if not T0 + 3 * 3600 <= s.t < T0 + 6 * 3600]
    assert not tank.covers(hole, T0, T0 + 86_400)
    late = [s for s in whole if s.t >= T0 + 4 * 3600]
    assert not tank.covers(late, T0, T0 + 86_400)


def test_the_first_day_is_never_before_the_raw_history() -> None:
    kept_from = T0 + 10 * 86_400 + 3600  # pruned up to an hour into the eleventh day
    assert tank.first_day(kept_from, DAY, UTC) == date(2026, 10, 26)
    assert tank.first_day(T0, None, UTC) == DAY
    assert tank.first_day(T0, DAY, UTC) == date(2026, 10, 16)


def test_the_water_is_the_same_on_every_day() -> None:
    body = {"t": T0, "supply": 40.0, "back": 32.0, "water": 40.0}
    assert _hour(body, back=True).water == 36.0
    assert _hour(body, back=False).water == 40.0
    no_return = {"t": T0, "supply": 40.0, "back": None, "water": 40.0}
    assert _hour(no_return, back=True).water is None  # not mixed in as supply alone
    old = {"t": T0, "supply": 40.0, "water": 38.0}
    assert _hour(old, back=True).water == 38.0  # kept before the return was: as it was
