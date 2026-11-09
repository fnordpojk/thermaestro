"""What a tank's household will draw, by what kind of day it is: home, away, a departure or
a return (`learn.occupied`), and, where it helps, a working day or a free one.

**What is predicted:** at a moment, for each three-hour block of the next two days, how much
will have been drawn from then to the block's end, in nine cases out of ten at most. Each
day ahead is taken as any one of the past days of its kind, every combination of them
weighed alike: the line is what nine in ten of those combinations stay within. The planner
gets it hour by hour, each block's rise spread over its hours, so the tank is ready by each
block's end for all but the heaviest tenth of such days.

**Its kind ahead:** away while an away request holds, a return on the day it ends, a
departure on the day one starts; else as whether anyone is home was last concluded.

**Too few days of a kind** (fewer than `LEAST_SAME`, as with a first trip): the working and
free days of it are taken together, and with fewer still, every day known.

**Judged** each day on the days before, as at each of their midnights: what the line was then,
from the days before it labeled as they were known then, against what was drawn in the two
days after. Scored by the quantile loss at nine in ten, against the week, a flat guess, the
same day last week and the last seven days' mean; and how often the draws stayed within it
is given beside, by kind of day, as the loss alone would reward a line set too high. Whether
working and free days are told apart is kept only where that scores better. It is used
instead of the week only once it beats the best of the others by the margin (`validate`).
"""

import itertools
import math
import random
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta, tzinfo

from .model import mark
from .occupied import DayLabel
from .validate import MARGIN, unjudged

Q = 0.9
"""The line's quantile: nine in ten."""
BLOCK_H = 3
HORIZON_H = 48
LEAST_SAME = 3
"""Days of a kind its own line needs; fewer, and more days are taken (see above)."""
POOL_DAYS = 56
"""How far back the past days are taken from."""
JUDGED_DAYS = 14
LEAST_JUDGED = 7
COMBINATIONS = 1024
"""At most this many combinations of past days are weighed; more are sampled, the same way
each time."""

FEW = unjudged(mark("%(n)s days judged so far, of %(least)s."))
PROVED = mark(
    "Its line misses by %(loss)s where the best other guess misses by %(other)s; drawn"
    " within it in %(within)s of %(blocks)s blocks, over %(days)s days."
)
NOT_BETTER = mark(
    "Its line misses by %(loss)s, not clearly less than the best other guess, by %(other)s;"
    " drawn within it in %(within)s of %(blocks)s blocks, over %(days)s days."
)


@dataclass(frozen=True, slots=True)
class Past:
    """A day as drawn: by local hour, at the year's mean inlet temperature."""

    day: date
    hourly: tuple[float, ...]
    kind: DayLabel
    free: bool


@dataclass(frozen=True, slots=True)
class Ahead:
    """A day the line reaches into."""

    day: date
    kind: DayLabel
    free: bool
    scale: float = 1.0
    """The season: how much more a draw takes than at the year's mean inlet."""


def pool(days: Sequence[Past], kind: DayLabel, free: bool, by_class: bool) -> list[Past]:
    """The past days a day of `kind` is taken as."""
    known = [d for d in days if d.kind != "unknown"]
    same = [d for d in known if d.kind == kind]
    if by_class:
        alike = [d for d in same if d.free == free]
        if len(alike) >= LEAST_SAME:
            return alike
    return same if len(same) >= LEAST_SAME else known


def quantile(values: Sequence[float], q: float = Q) -> float:
    ordered = sorted(values)
    return ordered[max(0, math.ceil(q * len(ordered)) - 1)]


def line(
    hours: Sequence[tuple[date, int]],
    ahead: Mapping[date, Ahead],
    days: Sequence[Past],
    by_class: bool,
) -> list[float] | None:
    """How much will have been drawn by the end of each block of `hours` (each a local day
    and hour), in nine of ten combinations of past days; None where a day has none."""
    order = sorted({d for d, _ in hours})
    pools = []
    for d in order:
        a = ahead[d]
        found = pool(days, a.kind, a.free, by_class)
        if not found:
            return None
        pools.append(found)
    combos: list[tuple[Past, ...]]
    total = math.prod(len(p) for p in pools)
    if total <= COMBINATIONS:
        combos = list(itertools.product(*pools))
    else:
        rng = random.Random(0)  # noqa: S311 - a sample, the same each time, not a secret
        combos = [tuple(rng.choice(p) for p in pools) for _ in range(COMBINATIONS)]
    index = {d: i for i, d in enumerate(order)}
    ends = list(range(BLOCK_H, len(hours) + 1, BLOCK_H))
    sums: list[list[float]] = [[] for _ in ends]
    for combo in combos:
        drawn = 0.0
        b = 0
        for i, (d, h) in enumerate(hours):
            drawn += combo[index[d]].hourly[h] * ahead[d].scale
            if b < len(ends) and i + 1 == ends[b]:
                sums[b].append(drawn)
                b += 1
    # Each combination's total only grows, so the line never falls from block to block.
    return [quantile(by_then) for by_then in sums]


def hourly(blocks: Sequence[float]) -> tuple[float, ...]:
    """The line hour by hour: each block's rise spread over its hours."""
    out: list[float] = []
    before = 0.0
    for c in blocks:
        out += [(c - before) / BLOCK_H] * BLOCK_H
        before = c
    return tuple(out)


def local_hours(start: float, zone: tzinfo, hours: int = HORIZON_H) -> list[tuple[date, int]]:
    """Each hour from `start`'s, as a local day and hour."""
    first = start // 3600 * 3600
    found = []
    for i in range(hours):
        when = datetime.fromtimestamp(first + i * 3600, zone)
        found.append((when.date(), when.hour))
    return found


def blocks(hourly: Sequence[float]) -> list[float]:
    """What was drawn by the end of each block."""
    out, drawn = [], 0.0
    for i, v in enumerate(hourly):
        drawn += v
        if (i + 1) % BLOCK_H == 0:
            out.append(drawn)
    return out


def pinball(actual: float, predicted: float, q: float = Q) -> float:
    """The quantile loss: a line too low costs `q` a unit, too high `1 - q`."""
    return q * (actual - predicted) if actual >= predicted else (1 - q) * (predicted - actual)


@dataclass(frozen=True, slots=True)
class Moment:
    """A midnight judged: what was drawn by each block's end in the two days after, its
    day's kind as known now, and each guess's line then."""

    actual: tuple[float, ...]
    kind: DayLabel
    guesses: Mapping[str, Sequence[float]]


@dataclass(frozen=True, slots=True)
class Scored:
    proved: bool
    by_class: bool
    why: str
    args: dict[str, float | int | str]


MODELS = ("plain", "by_class")
OTHERS = ("week", "flat", "last_week", "seven_days")


def score(moments: Sequence[Moment]) -> Scored:
    """The line against the others, on the moments every one of them had a guess for."""
    common = [m for m in moments if all(g in m.guesses for g in (*MODELS, *OTHERS))]
    if len(common) < LEAST_JUDGED:
        return Scored(False, False, FEW, {"n": len(common), "least": LEAST_JUDGED})

    def loss(name: str) -> float:
        found = [
            pinball(a, p) for m in common for a, p in zip(m.actual, m.guesses[name], strict=True)
        ]
        return sum(found) / len(found)

    by_class = loss("by_class") < loss("plain")
    chosen = "by_class" if by_class else "plain"
    mine, other = loss(chosen), min(loss(g) for g in OTHERS)
    within = [
        (m.kind, a <= p) for m in common for a, p in zip(m.actual, m.guesses[chosen], strict=True)
    ]
    args: dict[str, float | int | str] = {
        "loss": round(mine, 3),
        "other": round(other, 3),
        "within": sum(w for _, w in within),
        "blocks": len(within),
        "days": len(common),
    }
    for kind in sorted({m.kind for m in common}):
        mine_kind = [w for k, w in within if k == kind]
        args[f"within_{kind}"] = round(sum(mine_kind) / len(mine_kind), 2)
        args[f"days_{kind}"] = sum(1 for m in common if m.kind == kind)
    proved = mine <= other * (1 - MARGIN)
    return Scored(proved, by_class, PROVED if proved else NOT_BETTER, args)


def others(
    day: date,
    read: Mapping[date, tuple[float, ...]],
    predicted: Mapping[date, tuple[Sequence[float] | None, Sequence[float] | None]],
) -> dict[str, list[float]]:
    """The other guesses at `day`'s midnight, for it and the day after, by block: the week
    and the flat guess as each day kept them, the same days a week before, and the mean
    day of the seven before."""
    two = (day, day + timedelta(days=1))
    out: dict[str, list[float]] = {}
    week = [predicted.get(d, (None, None))[0] for d in two]
    flat = [predicted.get(d, (None, None))[1] for d in two]
    if all(w is not None for w in week):
        out["week"] = blocks([v for w in week for v in w or ()])
    if all(f is not None for f in flat):
        out["flat"] = blocks([v for f in flat for v in f or ()])
    before = [read.get(d - timedelta(days=7)) for d in two]
    if all(b is not None for b in before):
        out["last_week"] = blocks([v for b in before for v in b or ()])
    seven = [read[d] for i in range(1, 8) if (d := day - timedelta(days=i)) in read]
    if len(seven) >= 4:
        mean = [sum(h) / len(seven) for h in zip(*seven, strict=True)]
        out["seven_days"] = blocks(mean * 2)
    return out


def kinds_ahead(
    days: Sequence[date],
    zone: tzinfo,
    now: str | None,
    away: Callable[[datetime], bool],
    free: Callable[[date], bool],
) -> dict[date, tuple[DayLabel, bool]]:
    """Each day's kind ahead, by its waking hours: away while an away request holds (`away`),
    a return on the day it ends, a departure on the day one starts; else as last concluded
    (`now`), home where nothing has been."""
    from .occupied import WAKING

    out: dict[date, tuple[DayLabel, bool]] = {}
    for d in days:
        first = away(datetime.combine(d, datetime.min.time(), zone) + timedelta(hours=WAKING[0]))
        last = away(datetime.combine(d, datetime.min.time(), zone) + timedelta(hours=WAKING[1]))
        kind: DayLabel
        if first and last:
            kind = "away"
        elif first:
            kind = "return"
        elif last:
            kind = "departure"
        else:
            kind = "away" if now == "away" else "home"
        out[d] = (kind, free(d))
    return out
