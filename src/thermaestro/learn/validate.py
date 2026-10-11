"""Whether a learned model has earned its place: on data it wasn't fitted on, it must
predict better than a simple baseline (yesterday's value, the setup answer), by a margin.
A model that can't be judged yet isn't used either."""

from collections.abc import Sequence
from dataclasses import dataclass

MARGIN = 0.10
"""How much smaller its mean absolute error must be than the baseline's."""
LEAST = 24
"""How many held-out comparisons a verdict needs."""


@dataclass(frozen=True, slots=True)
class Verdict:
    proved: bool
    error: float | None
    """The model's mean absolute error on the held-out data."""
    baseline: float | None
    n: int
    why: str


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
        return Verdict(False, None, None, len(rows), f"{len(rows)} of {least} comparisons so far")
    error = sum(abs(p - a) for p, _, a in rows) / len(rows)
    base = sum(abs(b - a) for _, b, a in rows) / len(rows)
    if error <= base * (1 - margin):
        why = f"error {error:.3g} against the baseline's {base:.3g}"
        return Verdict(True, error, base, len(rows), why)
    why = f"error {error:.3g}, not {margin:.0%} below the baseline's {base:.3g}"
    return Verdict(False, error, base, len(rows), why)
