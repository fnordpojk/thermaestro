"""A grid company's rules as the household entered them: when each part of a rule holds,
a time-of-use rule's price at a moment, and how high the house's power may go now without
raising a power charge.

A rule's times are local: civil time with summer time, or, where the grid company says so,
normal time all year. A kind of day is that of the moment's own date, also for a time span
that runs past midnight.

The house's power in an interval is its mean over time, as its energy would be billed:
each reading holds until the next, for at most two of the history's heartbeats (a reading
is kept at least that often while its plugin runs), and an interval counts where readings
cover at least half of its time so far.
"""

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from ..intents.calendar import Calendar
from ..store import Database, GridRule, Home
from ..store.settings import When
from .values import SLOT_S, Key, Values

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


COVERED = 0.5
"""The share of an interval's time so far that readings must cover for it to count."""

Spread = dict[float, list[float]]
"""Per interval's start: its kW-seconds and the seconds readings cover."""


def interval_means(
    samples: Iterable[tuple[float, float | None]],
    minutes: int,
    start: float,
    end: float,
    *,
    hold_s: float,
) -> dict[float, float]:
    """The mean power in each interval of `minutes` from `start` to `end`, by the
    interval's start: over time, each reading (seconds since the epoch, and kW, or None
    where it wasn't good) holding until the next, for at most `hold_s`. Only intervals
    readings cover at least `COVERED` of, up to `end`."""
    width = minutes * 60.0
    return _means(_spread(samples, width, start, end, hold_s, {}), width, end)


def _spread(
    samples: Iterable[tuple[float, float | None]],
    width: float,
    start: float,
    end: float,
    hold_s: float,
    into: Spread,
) -> Spread:
    rows = sorted(samples, key=lambda r: r[0])
    for i, (t, kw) in enumerate(rows):
        if kw is None:
            continue
        until = rows[i + 1][0] if i + 1 < len(rows) else end
        t0, t1 = max(t, start), min(until, t + hold_s, end)
        while t0 < t1:
            at = t0 - t0 % width
            stop = min(t1, at + width)
            found = into.setdefault(at, [0.0, 0.0])
            found[0] += kw * (stop - t0)
            found[1] += stop - t0
            t0 = stop
    return into


def _means(spread: Spread, width: float, end: float) -> dict[float, float]:
    return {
        at: energy / seconds
        for at, (energy, seconds) in spread.items()
        if seconds >= COVERED * (min(at + width, end) - at)
    }


def peak_limit(
    rule: GridRule,
    means: Mapping[float, float],
    now: datetime,
    zone: ZoneInfo,
    holiday: Holiday | None = None,
) -> float | None:
    """How high the house's power may go in the interval now without raising the month's
    billed peak: the lowest of the month's highest intervals counted so far (the highest of
    each day, where they must fall on different days). None outside the rule's window, and
    until as many intervals as it averages have been counted."""
    here = local_time(now, zone, rule.clock)
    if rule.window and not any(holds(w, here, holiday) for w in rule.window):
        return None
    counted: dict[object, float] = {}
    for start, kw in means.items():
        local = local_time(datetime.fromtimestamp(start, UTC), zone, rule.clock)
        if (local.year, local.month) != (here.year, here.month):
            continue
        if rule.window and not any(holds(w, local, holiday) for w in rule.window):
            continue
        key: object = local.date() if rule.different_days else start
        counted[key] = max(counted.get(key, kw), kw)
    highest = sorted(counted.values(), reverse=True)
    n = rule.peaks or 1
    return highest[n - 1] if len(highest) >= n else None


@dataclass(frozen=True)
class GridLimit:
    """The most the house's power may draw now by the grid company's rules, and why."""

    kw: float
    why: str


def _measurable(rule: GridRule) -> bool:
    """Whether a power charge says enough to plan against: how it is measured, how many
    intervals count, and whether on different days."""
    return (
        rule.interval_minutes is not None
        and rule.peaks is not None
        and rule.different_days is not None
    )


class GridLimits:
    """The grid company's limit on the house's power now, from its rules and the house's
    power as measured: a subscribed power, or, in a power charge's window, the month's
    highest intervals so far. The lowest applies. A power charge that doesn't say how it
    is measured is left out. Days whose samples were folded into 15-minute means count
    from those."""

    def __init__(self, db: Database, values: Values) -> None:
        self._db = db
        self._values = values
        self._months: dict[tuple[Key, int], tuple[float, float, dict[float, float]]] = {}
        """Per power point and interval: the mean power per interval from the month's
        start to today's, kept so each round reads only today's history."""

    def house_power(self) -> tuple[Key, float] | None:
        """The house's power point, and what its values are multiplied by for kW."""
        for key, envelope in sorted(self._values.latest.items(), key=lambda kv: kv[0].instance):
            if key.point.endswith("grid.import.power") and envelope.quality == "good":
                return key, 0.001 if envelope.unit == "W" else 1.0
        return None

    async def now(self, now: datetime, zone: ZoneInfo) -> GridLimit | None:
        rules, holiday = await household(self._db, zone)
        found: list[GridLimit] = []
        for rule in rules.values():
            if not rule.in_force(local_time(now, zone, rule.clock).date()):
                continue
            if rule.type == "subscribed_power" and rule.kw is not None:
                found.append(GridLimit(rule.kw, f"{rule.owner}: the subscribed power"))
            elif rule.type == "interval_peak" and _measurable(rule):
                means = await self._means(rule.interval_minutes or 60, now, zone)
                kw = peak_limit(rule, means, now, zone, holiday)
                if kw is not None:
                    which = "highest" if rule.peaks == 1 else f"{rule.peaks} highest"
                    found.append(GridLimit(kw, f"{rule.owner}: under the month's {which} so far"))
        return min(found, key=lambda g: g.kw, default=None)

    async def _means(self, minutes: int, now: datetime, zone: ZoneInfo) -> dict[float, float]:
        found = self.house_power()
        if found is None:
            return {}
        key, factor = found
        local = now.astimezone(zone)
        first = local.replace(day=1, hour=0, minute=0, second=0, microsecond=0).timestamp()
        today = local.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        hold = 2 * self._values.heartbeat_s
        width = minutes * 60.0

        async def means(start: float, end: float) -> dict[float, float]:
            # The 15-minute means each hold their slot; the samples since, with the one
            # before them that may hold on into them.
            slots, folded_until = await self._values.folded(key, start, end)
            samples = await self._values.history(key, folded_until - hold, end)
            spread = _spread(
                ((t, mean * factor) for t, mean in slots), width, start, folded_until, SLOT_S, {}
            )
            good = [
                (s.t, s.value * factor if s.quality == "good" and s.value is not None else None)
                for s in samples
            ]
            return _means(_spread(good, width, folded_until, end, hold, spread), width, end)

        kept = self._months.get((key, minutes))
        if kept is None or kept[:2] != (first, today):
            kept = (first, today, await means(first, today))
            self._months[(key, minutes)] = kept
        return {**kept[2], **await means(today, now.timestamp())}
