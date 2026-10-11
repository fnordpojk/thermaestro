"""The compressor's electricity, from the house's meter: what it draws while it runs, by
what it serves, and how often it starts.

A pump may have no electricity meter of its own, so the draw is read where the house's
power steps. When the compressor starts, the house's power rises by what it draws (its
circulation pumps with it); when it stops, it falls by as much. Each step is the mean
over a few minutes after the edge less the mean over a few minutes before, leaving out the
minutes right at the edge (the start's ramp, the pumps' run-on). An edge with another
edge, or the addition, that close isn't read. The household's own load comes and goes
too, so the draw is the median of many steps, for each thing the compressor serves:
making hot water takes more than heating, at its higher temperature.

It is judged on the last two days' steps against the planner's guess it replaces.
"""

import bisect
import statistics
from collections.abc import Sequence
from dataclasses import dataclass
from functools import partial
from typing import Literal

from ..core.values import Sample
from .inputs import Span, spans
from .model import mark
from .tank import Series
from .validate import Verdict, held_out

WINDOW_S = 300.0
"""How long each mean, before and after an edge, runs."""
GAP_S = 90.0
"""The time at an edge left out of both means."""
CLEAR_S = GAP_S + WINDOW_S
"""How far another edge, or the addition, must be."""
SAMPLE_S = 15.0
PURPOSES = ("heating", "dhw", "pool")
LEAST_STEPS = 10
"""Steps a purpose needs before its draw is given."""
GUESS_KW = 2.0
"""What the planner reckoned a hot-water charge draws, until this is learned."""
TEST_S = 2 * 86_400.0

FEW = mark("%(steps)s steps of the house's power seen at the compressor's starts and stops.")

Edge = Literal["start", "stop"]


@dataclass(frozen=True, slots=True)
class Step:
    t: float
    edge: Edge
    purpose: str
    kw: float


def runs(compressor: Sequence[Sample], end: float) -> list[Span]:
    """When the compressor ran."""
    return spans(compressor, end, lambda s: s.text == "running")


def _text_at(samples: Sequence[Sample], times: Sequence[float], t: float) -> str | None:
    i = bisect.bisect_right(times, t) - 1
    if i < 0:
        return None
    s = samples[i]
    return s.text if s.quality == "good" else None


def _mean(house: Series, t0: float, t1: float) -> float | None:
    found = []
    t = t0
    while t < t1:
        value = house.at(t)
        if value is None:
            return None
        found.append(value)
        t += SAMPLE_S
    return sum(found) / len(found) if found else None


def steps(
    ran: Sequence[Span],
    house: Series,
    demand: Sequence[Sample],
    *,
    factor: float = 1.0,
    addition: Sequence[Span] = (),
    start: float = float("-inf"),
    end: float = float("inf"),
) -> list[Step]:
    """The house's power step at each start and stop between `start` and `end`, in kW
    (`factor` turns the meter's values into kW), where nothing else happened close by."""
    edges: list[tuple[float, Edge]] = sorted(
        [(t0, "start") for t0, _ in ran] + [(t1, "stop") for _, t1 in ran]
    )
    times = [s.t for s in demand]
    out = []
    for i, (t, edge) in enumerate(edges):
        if not start <= t < end:
            continue
        near = [u for u, _ in edges[max(0, i - 1) : i + 2] if u != t]
        if any(abs(u - t) < CLEAR_S for u in near):
            continue
        if any(a0 < t + CLEAR_S and t - CLEAR_S < a1 for a0, a1 in addition):
            continue
        before = _mean(house, t - CLEAR_S, t - GAP_S)
        after = _mean(house, t + GAP_S, t + CLEAR_S)
        if before is None or after is None:
            continue
        side = t + CLEAR_S / 2 if edge == "start" else t - CLEAR_S / 2
        purpose = _text_at(demand, times, side)
        if purpose not in PURPOSES:
            continue
        kw = (after - before if edge == "start" else before - after) * factor
        out.append(Step(t, edge, purpose, round(kw, 3)))
    return out


def draws(found: Sequence[Step]) -> dict[str, float]:
    """The draw for each purpose seen often enough: the median of its steps."""
    out = {}
    for purpose in PURPOSES:
        mine = [s.kw for s in found if s.purpose == purpose]
        if len(mine) >= LEAST_STEPS:
            out[purpose] = round(statistics.median(mine), 3)
    return out


def judge(found: Sequence[Step], test_from: float) -> Verdict:
    """The draws learned before `test_from`, against the guess they replace, on the steps
    since."""
    learned = draws([s for s in found if s.t < test_from])
    test = [s for s in found if s.t >= test_from and s.purpose in learned]
    return held_out(
        [learned[s.purpose] for s in test], [GUESS_KW] * len(test), [s.kw for s in test]
    )


def _serves(purpose: str, s: Sample) -> bool:
    return s.text == purpose


def starts(ran: Sequence[Span], start: float, end: float) -> int:
    return sum(1 for t0, _ in ran if start <= t0 < end)


def run_seconds(
    ran: Sequence[Span], demand: Sequence[Sample], start: float, end: float
) -> dict[str, float]:
    """How long the compressor ran for each purpose between `start` and `end`."""
    out: dict[str, float] = {}
    for purpose in PURPOSES:
        serving = spans(demand, end, partial(_serves, purpose))
        total = 0.0
        for r0, r1 in ran:
            for d0, d1 in serving:
                t0, t1 = max(r0, d0, start), min(r1, d1, end)
                if t0 < t1:
                    total += t1 - t0
        if total:
            out[purpose] = round(total)
    return out
