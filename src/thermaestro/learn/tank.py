"""The hot-water tank, learned from its two temperatures and the pump's demand.

Heat is counted in degrees of the tank: how far something takes the mean of its two
sensors (the top, and the charge sensor low in the tank). It becomes kWh once the tank's
volume is known; the planner works in these degrees directly.

- **Charges:** the demand on hot water for ten minutes or more. During a charge, and for
  15 minutes after, the charge sensor shows the charge, not the tank (when the charge
  pump starts, the water from the bottom of the tank plunges it by tens of degrees), so
  nothing else is read then.
- **Standing loss:** `k` per hour, times how far the tank's mean is above the room it
  stands in. From the slowest hours outside charges: a draw only adds to a fall, so a
  low share of the hours shows the loss alone.
- **Draws:** a fall faster than the standing loss, by more than the tank's own quiet
  noise. A draw that makes the pump start a charge counts up to the charge's start: that
  fall is the draw. A draw is booked to the hour with most of its fall.
- **Demand:** what is drawn per hour of the week (Monday 00:00 is hour 0), learned day by
  day: each day's hours blended into the week's, a public holiday as the weekday it
  counts as. A draw is compared across seasons at the year's mean cold-water temperature.
- **Ceiling:** the highest the top has reached at the end of a charge other than the
  periodic increase: what the compressor gives on its own, about.

Every day is judged before it is learned from: the demand the week predicted for it,
against a flat guess (the mean hour so far). That is the demand's proof.
"""

import bisect
import statistics
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, tzinfo

from ..core.values import Sample
from .inputs import HOLD_S, spans
from .validate import Verdict, held_out

STEP_S = 900.0
SETTLE_S = 900.0
CHARGE_LEAST_S = 600.0
ROOM_C = 20.0
"""Where a tank stands, about: what its standing loss is reckoned against."""
LOSS_SHARE = 0.25
"""The share of the quiet hours whose fall is taken as the standing loss alone."""
LOSS_LEAST = 24
"""How many quiet hours the standing loss needs."""
OPEN_LEAST = 0.3
"""The least fall beyond the standing loss, °C of the tank in a quarter, that opens a draw."""
NOISE_TIMES = 4.0
"""A draw opens at this many times the tank's quiet noise, when that is more."""
ALPHA = 0.1
"""How much a new day weighs in the week's demand, once a weekday has been seen often."""
PERIODIC_ABOVE = 3.0
HOURS = 168


@dataclass(frozen=True, slots=True)
class Charge:
    start: float
    end: float
    top0: float | None
    top1: float | None


@dataclass(frozen=True, slots=True)
class Draw:
    start: float
    end: float
    size: float
    """°C of the tank, beyond the standing loss."""
    at: float
    """The start of the quarter with most of its fall."""


@dataclass(frozen=True, slots=True)
class Day:
    day: date
    draws: tuple[Draw, ...]
    charges: tuple[Charge, ...]
    hourly: tuple[float, ...]
    """What was drawn in each of the day's 24 local hours, at the year's mean inlet."""


@dataclass
class Demand:
    """What is drawn per hour of the week, at the year's mean inlet temperature."""

    hours: list[float] = field(default_factory=lambda: [0.0] * HOURS)
    seen: list[int] = field(default_factory=lambda: [0] * 7)
    """How many of each weekday have been learned from."""

    def to_value(self) -> dict[str, float]:
        out = {str(i): round(v, 4) for i, v in enumerate(self.hours)}
        out |= {f"n{d}": float(n) for d, n in enumerate(self.seen)}
        return out

    @classmethod
    def from_value(cls, value: object) -> "Demand":
        if not isinstance(value, dict):
            return cls()
        hours = [float(value.get(str(i), 0.0)) for i in range(HOURS)]
        seen = [int(value.get(f"n{d}", 0)) for d in range(7)]
        return cls(hours, seen)

    def day(self, weekday: int) -> list[float]:
        return self.hours[weekday * 24 : weekday * 24 + 24]

    def learn(self, weekday: int, hourly: Sequence[float]) -> None:
        """Blend a day's 24 hours in: a running mean until the weekday has been seen
        often, then a weight of `ALPHA`."""
        self.seen[weekday] += 1
        weight = max(ALPHA, 1 / self.seen[weekday])
        for h, drawn in enumerate(hourly):
            i = weekday * 24 + h
            self.hours[i] += weight * (drawn - self.hours[i])

    @property
    def days(self) -> int:
        return sum(self.seen)


# --- reading a stretch of the tank ------------------------------------------------------------


class Series:
    """A point's samples in time order, read at any moment."""

    def __init__(self, samples: Sequence[Sample]) -> None:
        self.samples = list(samples)
        self._times = [s.t for s in self.samples]

    def at(self, t: float) -> float | None:
        """The good reading in force at `t`, if it is at most `HOLD_S` old."""
        i = bisect.bisect_right(self._times, t) - 1
        if i < 0:
            return None
        s = self.samples[i]
        if s.quality != "good" or s.value is None or t - s.t > HOLD_S:
            return None
        return s.value


def charges(demand: Sequence[Sample], end: float, top: Series) -> list[Charge]:
    found = []
    for t0, t1 in spans(demand, end, lambda s: s.text == "dhw"):
        if t1 - t0 >= CHARGE_LEAST_S:
            found.append(Charge(t0, t1, top.at(t0 - 1), top.at(t1)))
    return found


def _busy(t: float, found: Sequence[Charge]) -> bool:
    return any(c.start <= t < c.end + SETTLE_S for c in found)


def _mean(top: Series, low: Series, t: float) -> float | None:
    a, b = top.at(t), low.at(t)
    return None if a is None or b is None else (a + b) / 2


def steps(
    top: Series,
    low: Series,
    found: Sequence[Charge],
    start: float,
    end: float,
    step: float = STEP_S,
) -> list[tuple[float, float, float, float]]:
    """The quarters outside charges, each `(t0, t1, mean at t0, mean at t1)`. A quarter a
    charge starts in ends at the charge's start, read just before it."""
    out = []
    t = start - start % step
    while t < end:
        t1 = t + step
        if not _busy(t, found):
            cut = next((c.start for c in found if t < c.start < t1), None)
            stop = t1 if cut is None else cut
            m0 = _mean(top, low, t)
            m1 = _mean(top, low, stop - 1 if cut is not None else stop)
            if m0 is not None and m1 is not None and not _busy(stop - 1, found) and stop > t:
                out.append((t, stop, m0, m1))
        t = t1
    return out


def loss_rate(hours: Sequence[tuple[float, float, float, float]]) -> float | None:
    """`k`, per hour, from quiet hours: the fall over the mean's height above the room."""
    rates = []
    for t0, t1, m0, m1 in hours:
        above = (m0 + m1) / 2 - ROOM_C
        if above > 5 and t1 - t0 >= 0.9 * 3600:
            rates.append((m0 - m1) / ((t1 - t0) / 3600) / above)
    if len(rates) < LOSS_LEAST:
        return None
    rates.sort()
    return max(0.0, rates[int(LOSS_SHARE * (len(rates) - 1))])


def excess(step: tuple[float, float, float, float], k: float) -> float:
    t0, t1, m0, m1 = step
    lost = k * ((m0 + m1) / 2 - ROOM_C) * (t1 - t0) / 3600
    return (m0 - m1) - lost


def threshold(quarters: Sequence[tuple[float, float, float, float]], k: float) -> float:
    """What opens a draw: the tank's quiet noise, many times over, and at least
    `OPEN_LEAST`."""
    values = [abs(excess(q, k)) for q in quarters if q[1] - q[0] >= 0.9 * STEP_S]
    noise = statistics.median(values) if values else 0.0
    return max(OPEN_LEAST, NOISE_TIMES * noise)


def draws(
    quarters: Sequence[tuple[float, float, float, float]], k: float, opens: float
) -> list[Draw]:
    """Runs of quarters falling faster than the standing loss by at least half of `opens`,
    a quarter's pause allowed, that somewhere fall by `opens`. A charge ends a run."""
    out: list[Draw] = []
    run: list[tuple[tuple[float, float, float, float], float]] = []

    def close() -> None:
        while run and run[-1][1] < opens / 2:
            run.pop()  # a pause at the end isn't part of it
        if run and max(e for _, e in run) >= opens:
            size = sum(max(0.0, e) for _, e in run)
            peak = max(run, key=lambda r: r[1])[0][0]
            out.append(Draw(run[0][0][0], run[-1][0][1], round(size, 3), peak))
        run.clear()

    for q in quarters:
        e = excess(q, k)
        if run and abs(run[-1][0][1] - q[0]) >= 1:
            close()  # a charge or a gap came between
        if e >= opens / 2:
            run.append((q, e))
        elif run and run[-1][1] >= opens / 2:
            run.append((q, e))  # a pause of one quarter
        else:
            close()
    close()
    return out


# --- the days ---------------------------------------------------------------------------------


def bounds(day: date, zone: tzinfo) -> tuple[float, float]:
    start = datetime.combine(day, time(), zone)
    return start.timestamp(), datetime.combine(day + timedelta(days=1), time(), zone).timestamp()


def hourly(found: Sequence[Draw], day: date, zone: tzinfo, scale: float) -> tuple[float, ...]:
    """A day's draws by local hour, divided by `scale` (the season)."""
    out = [0.0] * 24
    for d in found:
        when = datetime.fromtimestamp(d.at, zone)
        if when.date() == day:
            out[when.hour] += d.size / scale
    return tuple(round(v, 4) for v in out)


def read_day(
    day: date,
    zone: tzinfo,
    top: Series,
    low: Series,
    found: Sequence[Charge],
    k: float,
    opens: float,
    scale: float,
) -> Day:
    """One local day: its charges and draws, and what was drawn hour by hour."""
    start, end = bounds(day, zone)
    quarters = steps(top, low, found, start - 2 * 3600, end)
    seen = [d for d in draws(quarters, k, opens) if start <= d.at < end]
    in_day = tuple(c for c in found if start <= c.start < end)
    return Day(day, tuple(seen), in_day, hourly(seen, day, zone, scale))


def judge(days: Sequence[tuple[Sequence[float], Sequence[float], Sequence[float]]]) -> Verdict:
    """The demand's proof: each day's hours as predicted, as the flat guess had them, and
    as drawn."""
    predicted = [p for d in days for p in d[0]]
    baseline = [b for d in days for b in d[1]]
    actual = [a for d in days for a in d[2]]
    return held_out(predicted, baseline, actual, least=7 * 24)


def expected(
    demand: Demand,
    weekday: Callable[[date], int],
    zone: tzinfo,
    season: Callable[[date], float],
    start: datetime,
    hours: int,
) -> tuple[float, ...]:
    """What the household is expected to draw in each of the `hours` from `start`'s hour."""
    first = start.timestamp() // 3600 * 3600
    out = []
    for i in range(hours):
        when = datetime.fromtimestamp(first + i * 3600, zone)
        day = when.date()
        out.append(demand.hours[weekday(day) * 24 + when.hour] * season(day))
    return tuple(out)
