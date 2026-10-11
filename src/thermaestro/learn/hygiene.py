"""What a learner may take from recorded values, and what it must leave.

- An interval counts only when its time is at least 95 % covered by good readings.
- A gap is never filled: a reading holds until the next one, for at most `hold_s`.
- A jump faster than the quantity can change is left out.
- A learned value moves only so far in one fit.
- What a value was learned from has a fingerprint; a changed one starts it over.

Everything here is causal: nothing at or after `end` changes what comes out.
"""

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from ..core.values import Sample

COVERAGE = 0.95
HOLD_S = 3600.0
"""How long a reading holds with nothing after it, where the learner doesn't say."""


@dataclass(frozen=True, slots=True)
class Interval:
    start: float
    mean: float
    covered: float
    """The share of the interval good readings cover."""


def intervals(
    samples: Sequence[Sample],
    start: float,
    end: float,
    step: float,
    *,
    hold_s: float = HOLD_S,
    coverage: float = COVERAGE,
) -> list[Interval | None]:
    """The time-weighted mean of good readings in each `step` from `start` to `end`; None
    for an interval less than `coverage` covered."""
    count = int((end - start) // step)
    totals = [0.0] * count
    seconds = [0.0] * count
    rows = [s for s in samples if s.t < end]
    for i, s in enumerate(rows):
        if s.quality != "good" or s.value is None:
            continue
        until = rows[i + 1].t if i + 1 < len(rows) else end
        t0, t1 = max(s.t, start), min(until, s.t + hold_s, start + count * step)
        while t0 < t1:
            slot = int((t0 - start) // step)
            stop = min(t1, start + (slot + 1) * step)
            totals[slot] += s.value * (stop - t0)
            seconds[slot] += stop - t0
            t0 = stop
    return [
        Interval(start + i * step, totals[i] / seconds[i], seconds[i] / step)
        if seconds[i] >= coverage * step
        else None
        for i in range(count)
    ]


def without_jumps(samples: Sequence[Sample], most_per_h: float) -> list[Sample]:
    """The samples with every good reading that moved faster than `most_per_h` from the
    last good one marked `out_of_range`, so it neither counts nor holds."""
    out: list[Sample] = []
    last: Sample | None = None
    for s in samples:
        if s.quality == "good" and s.value is not None and last is not None:
            hours = max(s.t - last.t, 60.0) / 3600
            if abs(s.value - (last.value or 0.0)) > most_per_h * hours:
                out.append(Sample(s.t, s.value, s.text, "out_of_range"))
                continue
        if s.quality == "good" and s.value is not None:
            last = s
        out.append(s)
    return out


def limited(old: float | None, new: float, most: float) -> float:
    """`new`, but at most `most` away from `old`."""
    if old is None:
        return new
    return min(max(new, old - most), old + most)


def fingerprint(parts: Mapping[str, object]) -> str:
    """A short digest of what a value was learned from."""
    text = json.dumps(parts, sort_keys=True, default=str)
    return hashlib.sha256(text.encode()).hexdigest()[:16]
