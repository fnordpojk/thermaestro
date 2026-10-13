"""How a climate system's supply follows the outdoor temperature and the heating offset:
the pump's curve, learned from what it does.

From the hours the compressor heats the house (half the hour at least), the hour's mean
supply is fitted as

    supply = c0 + c1 · outdoor + c2 · offset

`c1` is the curve's slope (below 0: colder outside, warmer water), `c2` what one step of
the offset adds. It needs the offset to have moved: at least two values, each held for
`HELD_LEAST` hours. It is judged on the last four days against the curve alone (the same
fit without the offset).

The water the house model is driven by (the mean of supply and return) is fitted too, over
every hour the pump may heat, with how much of it carries over from one hour to the next:
a slab holds its water for hours. It is fitted on following the water six hours ahead, as
the optimizer follows it: a slab's water barely moves in an hour, and fitted an hour ahead
it can come out as holding nearly all of it with an offset step worth twice the curve's.
It is judged the same way against the same fit without the offset.
"""

import itertools
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from .house import Hour, minimize
from .model import mark
from .validate import Verdict, held_out

HEATING_LEAST = 0.5
HELD_LEAST = 12
TEST_HOURS = 96

STILL = mark("The offset hasn't moved enough to learn what a step does.")
FEW = mark("%(hours)s hours of heating of %(least)s.")
LEAST = 48


@dataclass(frozen=True, slots=True)
class Response:
    c0: float
    c1: float
    c2: float
    rows: int

    def supply(self, outdoor: float, offset: float) -> float:
        return self.c0 + self.c1 * outdoor + self.c2 * offset

    def as_value(self) -> dict[str, float]:
        return {"c0": round(self.c0, 4), "c1": round(self.c1, 4), "c2": round(self.c2, 4)}


def _rows(hours: Sequence[Hour]) -> list[tuple[float, float, float]]:
    return [
        (h.outdoor, h.offset, h.supply)
        for h in hours
        if not h.skip
        and h.heating is not None
        and h.heating >= HEATING_LEAST
        and h.outdoor is not None
        and h.offset is not None
        and h.supply is not None
    ]


def _solve(rows: Sequence[tuple[list[float], float]]) -> list[float] | None:
    n = len(rows[0][0])
    m = [[0.0] * (n + 1) for _ in range(n)]
    for x, y in rows:
        for i in range(n):
            for j in range(n):
                m[i][j] += x[i] * x[j]
            m[i][n] += x[i] * y
    for i in range(n):
        pivot = max(range(i, n), key=lambda r: abs(m[r][i]))
        if abs(m[pivot][i]) < 1e-9:
            return None
        m[i], m[pivot] = m[pivot], m[i]
        for r in range(n):
            if r != i:
                f = m[r][i] / m[i][i]
                for c in range(i, n + 1):
                    m[r][c] -= f * m[i][c]
    return [m[i][n] / m[i][i] for i in range(n)]


def fit(hours: Sequence[Hour]) -> tuple[Response | None, str | None, dict[str, float | int | str]]:
    rows = _rows(hours)
    if len(rows) < LEAST:
        return None, FEW, {"hours": len(rows), "least": LEAST}
    held: dict[float, int] = {}
    for _, offset, _ in rows:
        held[round(offset)] = held.get(round(offset), 0) + 1
    if sum(1 for n in held.values() if n >= HELD_LEAST) < 2:
        return None, STILL, {}
    found = _solve([([1.0, o, f], s) for o, f, s in rows])
    if found is None or found[2] <= 0:
        return None, STILL, {}
    return Response(found[0], found[1], found[2], len(rows)), None, {}


def judge(
    hours: Sequence[Hour], test_from: float
) -> tuple[Response | None, Verdict | None, str | None, dict[str, float | int | str]]:
    """Fit on the hours before `test_from`, test on those after against the curve alone;
    then fit on all."""
    train = [h for h in hours if h.t < test_from]
    test = _rows([h for h in hours if h.t >= test_from])
    trained, why, args = fit(train)
    verdict = None
    if trained is not None:
        curve = _solve([([1.0, o], s) for o, _, s in _rows(train)])
        if curve is not None:
            predicted = [trained.supply(o, f) for o, f, _ in test]
            baseline = [curve[0] + curve[1] * o for o, _, _ in test]
            actual = [s for _, _, s in test]
            verdict = held_out(predicted, baseline, actual)
    whole, why_all, args_all = fit(hours)
    if whole is None:
        return None, verdict, why_all or why, args_all or args
    return whole, verdict, None, {}


@dataclass(frozen=True, slots=True)
class Water:
    """The water in the climate system, the mean of its supply and return over the hour:
    what the house model is driven by. From one hour to the next it keeps a share `a` of
    where it was and moves the rest by the outdoor temperature and the offset,

        water = a · water before + w0 + w1 · outdoor + w2 · offset

    Radiators hold little of it (`a` near 0); a slab in the floor holds most, so the
    offset shows in its water only over hours. Fitted over every hour the pump may heat,
    running or not, unlike the supply."""

    w0: float
    w1: float
    w2: float
    lift: float
    """How much warmer the supply runs than the water while the compressor heats: the
    COP is reckoned at the supply."""
    rows: int
    low: float = -10.0
    high: float = 10.0
    """The offsets it was fitted over: beyond them it is a guess."""
    a: float = 0.0

    def next(self, before: float, outdoor: float, offset: float) -> float:
        """The water an hour on."""
        return self.a * before + self.w0 + self.w1 * outdoor + self.w2 * offset

    def water(self, outdoor: float, offset: float) -> float:
        """Where the water settles, held at these."""
        return (self.w0 + self.w1 * outdoor + self.w2 * offset) / (1 - self.a)

    @property
    def step(self) -> float:
        """What one step of the offset adds to the water, once settled."""
        return self.w2 / (1 - self.a)

    def as_value(self) -> dict[str, float]:
        return {
            "w0": round(self.w0, 4),
            "w1": round(self.w1, 4),
            "w2": round(self.w2, 4),
            "a": round(self.a, 4),
            "lift": round(self.lift, 3),
            "low": self.low,
            "high": self.high,
        }

    @classmethod
    def from_value(cls, value: dict[str, float]) -> "Water":
        return cls(
            float(value["w0"]),
            float(value["w1"]),
            float(value["w2"]),
            float(value["lift"]),
            0,
            float(value.get("low", -10.0)),
            float(value.get("high", 10.0)),
            float(value.get("a", 0.0)),
        )


WATER_AHEAD = 6
"""Hours the water is followed ahead, to judge it."""
HOLDS_MOST = 0.99
"""Water that keeps more than this an hour isn't told apart from water that never moves."""
FOLLOW_STEPS = 1500
"""The search's steps, fitting the water six hours ahead."""


def _water_rows(hours: Sequence[Hour]) -> list[tuple[float, float, float, float]]:
    """Hour pairs: the outdoor temperature, the offset and the water before, and the water."""
    out = []
    for before, h in itertools.pairwise(hours):
        if h.t - before.t != 3600 or h.skip or before.water is None:
            continue
        if h.outdoor is None or h.offset is None or h.water is None:
            continue
        out.append((h.outdoor, h.offset, before.water, h.water))
    return out


def _fit_rows(
    rows: Sequence[tuple[float, float, float, float]], offset: bool
) -> tuple[float, float, float, float] | None:
    """w0, w1, w2 and a by least squares; without the offset, w2 is 0."""
    if offset:
        found = _solve([([1.0, o, f, b], w) for o, f, b, w in rows])
        if found is not None and 0.0 <= found[3] <= HOLDS_MOST:
            return found[0], found[1], found[2], found[3]
        instant = _solve([([1.0, o, f], w) for o, f, _, w in rows])
        return None if instant is None else (instant[0], instant[1], instant[2], 0.0)
    found = _solve([([1.0, o, b], w) for o, _, b, w in rows])
    if found is not None and 0.0 <= found[2] <= HOLDS_MOST:
        return found[0], found[1], 0.0, found[2]
    instant = _solve([([1.0, o], w) for o, _, _, w in rows])
    return None if instant is None else (instant[0], instant[1], 0.0, 0.0)


def _followed(
    hours: Sequence[Hour], start: tuple[float, float, float, float], offset: bool
) -> tuple[float, float, float, float]:
    """From the fit an hour ahead, the fit that follows the water best `WATER_AHEAD` hours
    ahead, by its squared misses; without the offset, w2 stays 0."""
    starts = range(0, len(hours) - WATER_AHEAD, 2)

    def cost(x: list[float]) -> float:
        w0, w1, a = x[0], x[1], x[-1]
        w2 = x[2] if offset else 0.0
        if not 0.0 <= a <= HOLDS_MOST or (offset and w2 <= 0):
            return 1e9
        misses = []
        for k in starts:
            found = _ahead(hours, k, lambda b, o, f: a * b + w0 + w1 * o + w2 * f)
            then = hours[k + WATER_AHEAD].water
            if found is not None and then is not None:
                misses.append((found - then) ** 2)
        return sum(misses) / len(misses) if misses else 1e9

    w0, w1, w2, a = start
    first = [w0, w1, w2, a] if offset else [w0, w1, a]
    if cost(first) >= 1e9:
        return start
    x = minimize(cost, first, steps=FOLLOW_STEPS)
    if cost(x) >= cost(first):
        return start
    return (x[0], x[1], x[2], x[3]) if offset else (x[0], x[1], 0.0, x[2])


def fit_water(
    hours: Sequence[Hour],
) -> tuple[Water | None, str | None, dict[str, float | int | str]]:
    rows = _water_rows(hours)
    if len(rows) < LEAST:
        return None, FEW, {"hours": len(rows), "least": LEAST}
    held: dict[float, int] = {}
    for _, offset, _, _ in rows:
        held[round(offset)] = held.get(round(offset), 0) + 1
    if sum(1 for n in held.values() if n >= HELD_LEAST) < 2:
        return None, STILL, {}
    found = _fit_rows(rows, offset=True)
    if found is None or found[2] <= 0:
        return None, STILL, {}
    w0, w1, w2, a = _followed(hours, found, offset=True)
    lifts = [
        h.supply - h.water
        for h in hours
        if not h.skip
        and h.heating is not None
        and h.heating >= HEATING_LEAST
        and h.supply is not None
        and h.water is not None
    ]
    lift = sum(lifts) / len(lifts) if lifts else 0.0
    offsets = [round(f) for _, f, _, _ in rows]
    return Water(w0, w1, w2, lift, len(rows), min(offsets), max(offsets), a), None, {}


def _ahead(
    hours: Sequence[Hour], k: int, step: Callable[[float, float, float], float]
) -> float | None:
    """The water `WATER_AHEAD` hours after `k`, from the water at `k` and what was measured."""
    water = hours[k].water
    if water is None:
        return None
    for i in range(k + 1, k + WATER_AHEAD + 1):
        h = hours[i] if i < len(hours) else None
        if h is None or h.t - hours[i - 1].t != 3600 or h.skip:
            return None
        if h.outdoor is None or h.offset is None:
            return None
        water = step(water, h.outdoor, h.offset)
    return water


def judge_water(
    hours: Sequence[Hour], test_from: float
) -> tuple[Water | None, Verdict | None, str | None, dict[str, float | int | str]]:
    """Fit on the hours before `test_from`; from each hour after, follow the water six hours
    ahead with what was measured, against the same fit without the offset. Then fit on all."""
    train = [h for h in hours if h.t < test_from]
    trained, why, args = fit_water(train)
    verdict = None
    if trained is not None:
        plain = _fit_rows(_water_rows(train), offset=False)
        if plain is not None:
            c0, c1, _, ca = _followed(train, plain, offset=False)

            def baseline(before: float, outdoor: float, offset: float) -> float:
                return ca * before + c0 + c1 * outdoor

            predicted, base, actual = [], [], []
            for k, h in enumerate(hours):
                if h.t < test_from or k + WATER_AHEAD >= len(hours):
                    continue
                then = hours[k + WATER_AHEAD].water
                predicted.append(_ahead(hours, k, trained.next))
                base.append(_ahead(hours, k, baseline))
                actual.append(then)
            verdict = held_out(predicted, base, actual)
    whole, why_all, args_all = fit_water(hours)
    if whole is None:
        return None, verdict, why_all or why, args_all or args
    return whole, verdict, None, {}


def lead(response: Response, now: float, ahead: float, most: int) -> int:
    """Offset steps that make the supply now what the curve gives for the outdoor
    temperature ahead: a slow system's heat then arrives for the weather it meets."""
    steps = response.c1 * (ahead - now) / response.c2
    return max(-most, min(most, round(steps)))
