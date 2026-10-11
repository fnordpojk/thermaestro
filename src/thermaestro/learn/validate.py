"""Whether a learned model has earned its place: on data it wasn't fitted on, it must
predict better than a simple baseline (a flat guess, the setup answer), by a margin. A
model that can't be judged yet isn't used either."""

from collections.abc import Sequence
from dataclasses import dataclass, field

from .model import mark

MARGIN = 0.10
"""How much smaller its mean absolute error must be than the baseline's."""
LEAST = 24
"""How many held-out comparisons a verdict needs."""

FEW = mark("%(n)s of %(least)s comparisons so far.")
PROVED = mark("Off by %(error)s on average where a simple guess is off by %(baseline)s.")
NOT_BETTER = mark(
    "Off by %(error)s on average, not clearly better than a simple guess, off by %(baseline)s."
)


@dataclass(frozen=True, slots=True)
class Verdict:
    proved: bool
    error: float | None
    """The model's mean absolute error on the held-out data."""
    baseline: float | None
    n: int
    why: str
    """As a message for the web UI's catalog, with `args`."""
    args: dict[str, float | int | str] = field(default_factory=dict)


def held_out(
    predicted: Sequence[float | None],
    baseline: Sequence[float | None],
    actual: Sequence[float | None],
    *,
    margin: float = MARGIN,
    least: int = LEAST,
) -> Verdict:
    """Compare where all three are known."""
    rows = [
        (p, b, a)
        for p, b, a in zip(predicted, baseline, actual, strict=True)
        if p is not None and b is not None and a is not None
    ]
    if len(rows) < least:
        return Verdict(False, None, None, len(rows), FEW, {"n": len(rows), "least": least})
    error = sum(abs(p - a) for p, _, a in rows) / len(rows)
    base = sum(abs(b - a) for _, b, a in rows) / len(rows)
    args: dict[str, float | int | str] = {"error": round(error, 3), "baseline": round(base, 3)}
    if error <= base * (1 - margin):
        return Verdict(True, error, base, len(rows), PROVED, args)
    return Verdict(False, error, base, len(rows), NOT_BETTER, args)
