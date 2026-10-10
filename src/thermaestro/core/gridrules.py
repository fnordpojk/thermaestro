"""A grid company's rules as the household entered them: when each part of a rule holds,
and a time-of-use rule's price at a moment.

A rule's times are local: civil time with summer time, or, where the grid company says so,
normal time all year. A kind of day is that of the moment's own date, also for a time span
that runs past midnight.
"""

from collections.abc import Callable
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from ..intents.calendar import Calendar
from ..store import Database, GridRule, Home
from ..store.settings import When

Holiday = Callable[[date], bool]


def local_time(at: datetime, zone: ZoneInfo, clock: str) -> datetime:
    """The moment on the rule's clock: in normal time, summer time's hour is taken off."""
    local = at.astimezone(zone)
    if clock == "normal":
        local -= local.dst() or timedelta(0)
    return local


def holds(when: When, local: datetime, holiday: Holiday | None = None) -> bool:
    if when.months and local.month not in when.months:
        return False
    day = local.date()
    weekend = day.weekday() >= 5
    off = weekend or (holiday is not None and holiday(day))
    if when.days == "working_days" and off:
        return False
    if when.days == "non_working_days" and not off:
        return False
    if when.days == "weekdays" and weekend:
        return False
    if when.days == "weekends" and not weekend:
        return False
    return _within(local.time(), when.start, when.end)


def _within(t: time, start: time | None, end: time | None) -> bool:
    if start is None and end is None:
        return True
    if end is None:
        return t >= (start or time(0))
    if start is None:
        return t < end
    if start <= end:
        return start <= t < end
    return t >= start or t < end  # past midnight


def tou_price(
    rule: GridRule, at: datetime, zone: ZoneInfo, holiday: Holiday | None = None
) -> float | None:
    """A time-of-use rule's price per kWh at a moment: the first of its rates that holds,
    else its base. Nothing is charged by a rule that is paused, withdrawn or only announced;
    one in force but not on that day gives no price, since what applies then isn't known."""
    local = local_time(at, zone, rule.clock)
    if rule.status != "in_force":
        return 0.0
    if not rule.in_force(local.date()):
        return None
    for rate in rule.rates:
        if holds(rate, local, holiday):
            return rate.price
    return rule.base


async def household(db: Database, zone: ZoneInfo) -> tuple[dict[str, GridRule], Holiday]:
    """The grid rules entered, and the public holidays they count."""
    home = await db.get(Home) or Home()
    return await db.all(GridRule), Calendar(zone, home.holidays, home.holidays_as).holiday
