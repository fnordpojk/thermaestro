"""Whether each input a learner would use works on this installation.

A register that exists isn't a measurement: some answer 0, or one value forever, or
something other than their name says. So before a learner relies on a point, the point
must have behaved as its quantity would over the last week:
- it was recorded for at least a day, and gave good values;
- it changed, where its quantity changes;
- it agreed with the points it goes with: the compressor runs only while there is a
  demand; the tank warms during hot-water charges; the supply is warmer while the
  compressor heats the house; the house draws more while the compressor runs.

The verdict is usable; not usable, with what was seen; or not yet, with what hasn't been
seen (no hot-water charge in the week). Nothing here does more than read what is recorded.
Why is said as a message for the web UI's catalog, with its values apart.
"""

from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Literal

from .. import durations
from ..core.values import Key, Sample, Values
from .model import mark

WINDOW_S = 7 * 86_400.0
LEAST_S = 86_400.0
"""How long a point must have been recorded before it is judged."""
HOLD_S = 3600.0
"""How long a reading holds with nothing after it."""
CHARGE_LEAST_S = 600.0
"""A shorter hot-water run isn't judged as a charge."""
SEEN_LEAST_S = 1800.0
"""How long a state must have been seen before a mean over it says anything."""

Verdict = Literal["usable", "not_usable", "not_yet"]
Args = Mapping[str, float | int | str]


@dataclass(frozen=True, slots=True)
class Check:
    instance: str
    point: str
    verdict: Verdict
    why: str
    checked: float
    args: Args = field(default_factory=dict)
    """The values `why` names, as `%(name)s`."""


@dataclass(frozen=True, slots=True)
class Context:
    """The points an input is judged against, where the device has them."""

    demand: Key | None = None
    compressor: Key | None = None
    house: tuple[Key, float] | None = None
    """The house's power, and what its values are multiplied by for kW."""


class Recorded:
    """The week's samples, each point read once per round of checks."""

    def __init__(self, values: Values, start: float, end: float) -> None:
        self._values = values
        self.start, self.end = start, end
        self._read: dict[Key, list[Sample]] = {}

    async def get(self, key: Key) -> list[Sample]:
        if key not in self._read:
            self._read[key] = await self._values.history(key, self.start, self.end)
        return self._read[key]


Judged = tuple[Verdict, str, Args]
Relation = Callable[[list[Sample], Recorded, Context], Awaitable[Judged | None]]


@dataclass(frozen=True, slots=True)
class Expect:
    moves: bool = True
    relation: Relation | None = None


USABLE = mark("Behaved as it should in the last week.")


async def judge(
    key: Key, expect: Expect, recorded: Recorded, context: Context, now: float
) -> Check:
    samples = await recorded.get(key)
    verdict, why, args = _basics(samples, expect.moves, recorded.end)
    if verdict == "usable" and expect.relation is not None:
        found = await expect.relation(samples, recorded, context)
        if found is not None:
            verdict, why, args = found
    return Check(key.instance, key.point, verdict, why, now, args)


def _basics(samples: Sequence[Sample], moves: bool, end: float) -> Judged:
    if not samples:
        return "not_yet", mark("Nothing recorded in the last week."), {}
    span = end - samples[0].t
    if span < LEAST_S:
        why = mark("Recorded for %(span)s; a day is needed.")
        return "not_yet", why, {"span": durations.text(span // 60 * 60)}
    good = [s for s in samples if s.quality == "good" and (s.value, s.text) != (None, None)]
    if not good:
        why = mark("No good value in the last week, only: %(qualities)s.")
        return "not_usable", why, {"qualities": ", ".join(sorted({s.quality for s in samples}))}
    if moves:
        seen = {s.value if s.value is not None else s.text for s in good}
        if len(seen) < 2:
            (only,) = seen
            if only == 0:
                return (
                    "not_usable",
                    mark("Always 0 in the last week: an answer of 0 says nothing."),
                    {},
                )
            shown = only if only is not None else ""
            return "not_usable", mark("Always %(value)s in the last week."), {"value": shown}
    return "usable", USABLE, {}


# --- relations -------------------------------------------------------------------------------


async def follows_demand(
    samples: list[Sample], recorded: Recorded, context: Context
) -> Judged | None:
    """The compressor runs only while there is a demand: hot water, heating, a pool."""
    if context.demand is None:
        return None
    running = spans(samples, recorded.end, lambda s: s.text == "running")
    if seconds(running) < CHARGE_LEAST_S:
        return "not_yet", mark("The compressor wasn't seen running in the last week."), {}
    demand = await recorded.get(context.demand)
    idle = spans(demand, recorded.end, lambda s: s.text == "idle")
    share = overlap(running, idle) / seconds(running)
    if share > 0.2:
        why = mark("Running while the demand said idle, for %(share)s %% of its running time.")
        return "not_usable", why, {"share": round(share * 100)}
    return None


async def warms_in_charges(
    samples: list[Sample], recorded: Recorded, context: Context
) -> Judged | None:
    """A hot-water tank's temperatures rise while the compressor charges it."""
    if context.demand is None:
        return None
    demand = await recorded.get(context.demand)
    charges = [
        (t0, t1)
        for t0, t1 in spans(demand, recorded.end, lambda s: s.text == "dhw")
        if t1 - t0 >= CHARGE_LEAST_S
    ]
    rises = []
    for t0, t1 in charges:
        first, last = value_at(samples, t0), value_at(samples, t1)
        if first is not None and last is not None:
            rises.append(last - first)
    if not rises:
        return "not_yet", mark("No hot-water charge seen in the last week."), {}
    warmed = sum(1 for r in rises if r >= 0.3)
    if warmed * 2 < len(rises):
        why = mark("Rose in %(warmed)s of %(charges)s hot-water charges.")
        return "not_usable", why, {"warmed": warmed, "charges": len(rises)}
    return None


async def warmer_when_heating(
    samples: list[Sample], recorded: Recorded, context: Context
) -> Judged | None:
    """The supply is warmer while the compressor heats the house than while it stands."""
    if context.demand is None or context.compressor is None:
        return None
    compressor = await recorded.get(context.compressor)
    demand = await recorded.get(context.demand)
    running = spans(compressor, recorded.end, lambda s: s.text == "running")
    stopped = spans(compressor, recorded.end, lambda s: s.text == "stopped")
    heating = intersect(running, spans(demand, recorded.end, lambda s: s.text == "heating"))
    warm, cold = mean_in(samples, heating, recorded.end), mean_in(samples, stopped, recorded.end)
    if warm is None:
        why = mark("The compressor wasn't seen heating the house in the last week.")
        return "not_yet", why, {}
    if cold is None:
        return "not_yet", mark("The compressor wasn't seen standing in the last week."), {}
    if warm - cold < 1.0:
        why = mark("Only %(kelvin)s K warmer while the compressor heats than while it stands.")
        return "not_usable", why, {"kelvin": round(warm - cold, 1)}
    return None


async def draws_with_compressor(
    samples: list[Sample], recorded: Recorded, context: Context
) -> Judged | None:
    """The house draws more while the compressor runs."""
    if context.compressor is None or context.house is None:
        return None
    factor = context.house[1]
    compressor = await recorded.get(context.compressor)
    running = spans(compressor, recorded.end, lambda s: s.text == "running")
    stopped = spans(compressor, recorded.end, lambda s: s.text == "stopped")
    on, off = mean_in(samples, running, recorded.end), mean_in(samples, stopped, recorded.end)
    if on is None or off is None:
        why = mark("The compressor wasn't seen both running and standing in the last week.")
        return "not_yet", why, {}
    more = (on - off) * factor
    if more < 0.3:
        why = mark("Only %(kw)s kW more while the compressor runs than while it stands.")
        return "not_usable", why, {"kw": round(more, 2)}
    return None


async def runs_and_stops(
    samples: list[Sample], recorded: Recorded, context: Context
) -> Judged | None:
    """The addition's power: 0 while it is off, more while it runs. Not running in a week is
    nothing wrong; it just hasn't been seen."""
    good = [s.value for s in samples if s.quality == "good" and s.value is not None]
    if all(v == 0 for v in good):
        return "not_yet", mark("The addition wasn't seen running in the last week."), {}
    if all(v != 0 for v in good):
        return "not_usable", mark("Never 0 in the last week: never seen off."), {}
    return None


# --- time ------------------------------------------------------------------------------------


Span = tuple[float, float]


def spans(samples: Sequence[Sample], end: float, holds: Callable[[Sample], bool]) -> list[Span]:
    """When `holds` was true of the reading in force, each reading lasting until the next
    or `HOLD_S`."""
    out: list[Span] = []
    for i, s in enumerate(samples):
        if s.quality != "good" or not holds(s):
            continue
        until = samples[i + 1].t if i + 1 < len(samples) else end
        t1 = min(until, s.t + HOLD_S, end)
        if t1 <= s.t:
            continue
        if out and out[-1][1] >= s.t:
            out[-1] = (out[-1][0], max(out[-1][1], t1))
        else:
            out.append((s.t, t1))
    return out


def seconds(found: Iterable[Span]) -> float:
    return sum(t1 - t0 for t0, t1 in found)


def intersect(a: Sequence[Span], b: Sequence[Span]) -> list[Span]:
    out = []
    for a0, a1 in a:
        for b0, b1 in b:
            t0, t1 = max(a0, b0), min(a1, b1)
            if t0 < t1:
                out.append((t0, t1))
    return out


def overlap(a: Sequence[Span], b: Sequence[Span]) -> float:
    return seconds(intersect(a, b))


def mean_in(samples: Sequence[Sample], within: Sequence[Span], end: float) -> float | None:
    """The time-weighted mean of good readings inside `within`; None under `SEEN_LEAST_S`."""
    total = weight = 0.0
    for t0, t1, value in _held(samples, end):
        for w0, w1 in within:
            held = min(t1, w1) - max(t0, w0)
            if held > 0:
                total += value * held
                weight += held
    return total / weight if weight >= SEEN_LEAST_S else None


def value_at(samples: Sequence[Sample], t: float) -> float | None:
    """The good reading in force at `t`."""
    found = None
    for s in samples:
        if s.t > t:
            break
        found = s
    if found is None or found.quality != "good" or found.value is None:
        return None
    return found.value if t - found.t <= HOLD_S else None


def _held(samples: Sequence[Sample], end: float) -> Iterable[tuple[float, float, float]]:
    for i, s in enumerate(samples):
        if s.quality != "good" or s.value is None:
            continue
        until = samples[i + 1].t if i + 1 < len(samples) else end
        t1 = min(until, s.t + HOLD_S, end)
        if t1 > s.t:
            yield s.t, t1, s.value
