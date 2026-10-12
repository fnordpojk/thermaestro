"""How a climate system's supply follows the outdoor temperature and the heating offset:
the pump's curve, learned from what it does.

From the hours the compressor heats the house (half the hour at least), the hour's mean
supply is fitted as

    supply = c0 + c1 · outdoor + c2 · offset

`c1` is the curve's slope (below 0: colder outside, warmer water), `c2` what one step of
the offset adds. It needs the offset to have moved: at least two values, each held for
`HELD_LEAST` hours. It is judged on the last four days against the curve alone (the same
fit without the offset).

The water the house model is driven by (the mean of supply and return) is fitted the same
way over every hour the pump may heat, for the optimizer to steer the house through.
"""

from collections.abc import Sequence
from dataclasses import dataclass

from .house import Hour
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
    """The water in the climate system, the mean of its supply and return over the hour,
    by the outdoor temperature and the offset: what the house model is driven by. Unlike
    the supply, it is fitted over every hour the pump may heat, running or not."""

    w0: float
    w1: float
    w2: float
    lift: float
    """How much warmer the supply runs than the water while the compressor heats: the
    COP is reckoned at the supply."""
    rows: int

    def water(self, outdoor: float, offset: float) -> float:
        return self.w0 + self.w1 * outdoor + self.w2 * offset

    def as_value(self) -> dict[str, float]:
        return {
            "w0": round(self.w0, 4),
            "w1": round(self.w1, 4),
            "w2": round(self.w2, 4),
            "lift": round(self.lift, 3),
        }


def _water_rows(hours: Sequence[Hour]) -> list[tuple[float, float, float]]:
    return [
        (h.outdoor, h.offset, h.water)
        for h in hours
        if not h.skip and h.outdoor is not None and h.offset is not None and h.water is not None
    ]


def fit_water(
    hours: Sequence[Hour],
) -> tuple[Water | None, str | None, dict[str, float | int | str]]:
    rows = _water_rows(hours)
    if len(rows) < LEAST:
        return None, FEW, {"hours": len(rows), "least": LEAST}
    held: dict[float, int] = {}
    for _, offset, _ in rows:
        held[round(offset)] = held.get(round(offset), 0) + 1
    if sum(1 for n in held.values() if n >= HELD_LEAST) < 2:
        return None, STILL, {}
    found = _solve([([1.0, o, f], w) for o, f, w in rows])
    if found is None or found[2] <= 0:
        return None, STILL, {}
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
    return Water(found[0], found[1], found[2], lift, len(rows)), None, {}


def judge_water(
    hours: Sequence[Hour], test_from: float
) -> tuple[Water | None, Verdict | None, str | None, dict[str, float | int | str]]:
    """As `judge`, for the water."""
    train = [h for h in hours if h.t < test_from]
    test = _water_rows([h for h in hours if h.t >= test_from])
    trained, why, args = fit_water(train)
    verdict = None
    if trained is not None:
        curve = _solve([([1.0, o], w) for o, _, w in _water_rows(train)])
        if curve is not None:
            predicted = [trained.water(o, f) for o, f, _ in test]
            baseline = [curve[0] + curve[1] * o for o, _, _ in test]
            actual = [w for _, _, w in test]
            verdict = held_out(predicted, baseline, actual)
    whole, why_all, args_all = fit_water(hours)
    if whole is None:
        return None, verdict, why_all or why, args_all or args
    return whole, verdict, None, {}


def lead(response: Response, now: float, ahead: float, most: int) -> int:
    """Offset steps that make the supply now what the curve gives for the outdoor
    temperature ahead: a slow system's heat then arrives for the weather it meets."""
    steps = response.c1 * (ahead - now) / response.c2
    return max(-most, min(most, round(steps)))
