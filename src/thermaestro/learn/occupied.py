"""Whether anyone was home, hour by hour and day by day, as the learners read it.

From what occupancy had concluded by the moment the learner reads for, each conclusion
dated from when what it concluded began: an absence concluded after twelve quiet hours is
away from the last activity, so hours that read as home before it was concluded read as
away once it is. A later conclusion overrides the earlier ones from when it began, so what
was put right later is what is read; nothing concluded after that moment is used.

Before occupancy was weighed live (an installation with history from before it), the draws
the tank's learner kept are weighed by the same rules, as for a house whose only source is
its draws: home from a draw; away once every tank was read and quiet for `QUIET_ONE_S`, the
household's quiet hours not counted, dated from the last draw. A day a tank wasn't read is
not known, and the quiet starts again after it.

A day is labeled by its waking hours (`WAKING`, in the house's time): away when nobody was
home at their start and at their end; home when someone was at both, out between or not
(at work, at school); a departure from home to away, and a return from away to home. A day
with a waking hour not known is not known. A visit during an absence (the cat sitter)
leaves the day away.
"""

from bisect import bisect_right
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, tzinfo
from itertools import pairwise
from typing import TYPE_CHECKING, Literal

from ..occupancy import QUIET_ONE_S, State
from ..store import Database, Transaction
from . import records
from .records import DRAW, TANK_DAY, TankDay
from .tank import bounds

if TYPE_CHECKING:
    from ..intents import Intents

DayLabel = Literal["home", "away", "departure", "return", "unknown"]

WAKING = (7, 22)
"""The waking hours a day is labeled by, from and to these hours in the house's time."""
SETTLE_S = 7 * 86_400.0
"""How long before the first hour asked for the kept draws are weighed from, so the state
at that hour comes from the draws before it."""
STEP_S = 900.0
"""The step the kept draws are weighed in."""


@dataclass(frozen=True, slots=True)
class Timeline:
    """Who was home: each state from its start until the next."""

    starts: tuple[float, ...] = ()
    states: tuple[State, ...] = ()

    def at(self, t: float) -> State:
        i = bisect_right(self.starts, t) - 1
        return self.states[i] if i >= 0 else "unknown"

    def _held(self, t: float) -> dict[State, float]:
        edges = [t, *(s for s in self.starts if t < s < t + 3600), t + 3600]
        held: dict[State, float] = {}
        for a, b in pairwise(edges):
            state = self.at(a)
            held[state] = held.get(state, 0.0) + b - a
        return held

    def hour(self, t: float) -> State:
        """The state through most of the hour from `t`; the earlier one where it is half."""
        held = self._held(t)
        return max(held, key=lambda s: held[s])

    def share(self, t: float) -> float | None:
        """The share of the hour from `t` someone was home, of the part known; None where
        most of it isn't known."""
        if self.hour(t) == "unknown":
            return None
        held = self._held(t)
        home, away = held.get("home", 0.0), held.get("away", 0.0)
        return home / (home + away)

    def day(self, day: date, zone: tzinfo) -> DayLabel:
        hours = [
            self.hour(datetime.combine(day, time(h), zone).timestamp())
            for h in range(WAKING[0], WAKING[1])
        ]
        if "unknown" in hours:
            return "unknown"
        first, last = hours[0], hours[-1]
        if first == last:
            return first
        return "departure" if first == "home" else "return"


Row = tuple[float, float | None, State]
"""A conclusion: when it was concluded, when what it concludes began, and the state."""


def line(rows: Sequence[Row], now: float | None = None) -> Timeline:
    """The conclusions reached by `now`, in the order they were reached, each from when what
    it concluded began, over the earlier ones."""
    kept: list[tuple[float, State]] = []
    for concluded, began, state in rows:
        if now is not None and concluded > now:
            continue
        start = min(began, concluded) if began is not None else concluded
        kept = [(t, s) for t, s in kept if t < start]
        kept.append((start, state))
    starts: list[float] = []
    states: list[State] = []
    for t, s in kept:
        if not states or states[-1] != s:
            starts.append(t)
            states.append(s)
    return Timeline(tuple(starts), tuple(states))


def weigh(
    draws: Sequence[float],
    read: Sequence[tuple[float, float]],
    start: float,
    until: float,
    quiet: Callable[[float], bool],
) -> list[Row]:
    """The kept draws from `start` to `until`, weighed as occupancy weighs a house whose
    only source is its draws. `read`: the spans every tank was read in, in order."""
    found: list[Row] = []
    state: State | None = None
    counted = 0.0
    last: float | None = None
    draws = sorted(d for d in draws if start <= d < until)
    j = 0
    t = start
    while t < until:
        end = min(t + STEP_S, until)
        drew = False
        while j < len(draws) and draws[j] < end:
            last, drew, counted = draws[j], True, 0.0
            if state != "home":
                found.append((last, last, "home"))
                state = "home"
            j += 1
        if not drew:
            if not any(a <= t and end <= b for a, b in read):
                counted = 0.0
            elif not quiet(t):
                counted += end - t
            if counted >= QUIET_ONE_S and state != "away":
                found.append((end, last if last is not None else end, "away"))
                state = "away"
        t = end
    return found


async def timeline(
    db: Database,
    now: float,
    since: float,
    tanks: Sequence[str],
    zone: tzinfo,
    intents: "Intents | None" = None,
) -> Timeline:
    """Who was home from `since`, as concluded by `now`: before occupancy concluded
    anything, from the draws the tanks `tanks` kept."""
    return line(await history(db, since, now, tanks, zone, intents), now)


async def history(
    db: Database,
    since: float,
    until: float,
    tanks: Sequence[str],
    zone: tzinfo,
    intents: "Intents | None" = None,
) -> list[Row]:
    """Every conclusion, each with when it was (or, from the kept draws, would have been)
    reached: `line` gives who was home as known at any moment from them."""

    def concluded(t: Transaction) -> list[Row]:
        return list(t.execute("SELECT concluded, began, state FROM occupancy ORDER BY id"))

    live = await db.run(concluded)
    first = live[0] if live else None
    begun = min(first[0], first[1] if first[1] is not None else first[0]) if first else until
    live_from = min(begun, until)
    start = since - SETTLE_S
    before: list[Row] = []
    if tanks and start < live_from:
        draws, read = await _kept(db, tanks, start, zone)
        before = weigh(draws, read, start, live_from, await _quiet(intents))
    return [*before, *live]


async def _kept(
    db: Database, tanks: Sequence[str], since: float, zone: tzinfo
) -> tuple[list[float], list[tuple[float, float]]]:
    """The draws the tanks kept from `since`, and the spans every one of them was read in."""
    draws: list[float] = []
    read: list[tuple[float, float]] | None = None
    for scope in tanks:
        draws += [at for at, _ in await records.read(db, scope, DRAW, since)]
        days = await records.read(db, scope, TANK_DAY, since - 86_400)
        spans = _merged([bounds(d.day, zone) for _, d in days if isinstance(d, TankDay)])
        read = spans if read is None else _both(read, spans)
    return draws, read or []


def _merged(spans: list[tuple[float, float]]) -> list[tuple[float, float]]:
    out: list[tuple[float, float]] = []
    for a, b in sorted(spans):
        if out and a <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], b))
        else:
            out.append((a, b))
    return out


def _both(
    one: list[tuple[float, float]], other: list[tuple[float, float]]
) -> list[tuple[float, float]]:
    return [(max(a, c), min(b, d)) for a, b in one for c, d in other if max(a, c) < min(b, d)]


async def _quiet(intents: "Intents | None") -> Callable[[float], bool]:
    """Whether the household's quiet hours are in force at a moment, as the intents give
    them now: asked once a quarter hour."""
    if intents is None:
        return lambda t: False
    resolver = await intents.resolver()
    asked: dict[float, bool] = {}

    def quiet(t: float) -> bool:
        slot = t // STEP_S
        if slot not in asked:
            force = resolver.in_force(datetime.fromtimestamp(t, UTC))
            bound = force.bound("quiet", "house")
            asked[slot] = bound is not None and bound.value is True
        return asked[slot]

    return quiet
