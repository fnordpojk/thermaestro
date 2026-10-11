"""How far the weather forecasts miss at this house, learned from what Weather keeps: each
provider's temperature forecast for whole hours 1, 6, 24 and 48 hours ahead, beside the
outdoor reference measured then.

- **The bias**, per provider and lead time: the mean of forecast less measured. It is
  judged on the last week against the forecast as given, the bias from the weeks before;
  once proved, the planner takes it off the forecast it uses, in between the lead times
  by straight lines.
- **The spread**, per provider and lead time: how far the forecast was off nine times in
  ten, over the last 30 days, the bias taken off. Kept for the optimizer, which will size
  the margin on goals marked must from it; used as seen.
"""

import itertools
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from .. import clock
from ..store import Database, Transaction
from .model import Learned, State, mark
from .store import Learnings
from .validate import Verdict, held_out

BIAS = "forecast.bias"
SPREAD = "forecast.spread"
QUANTITY = "temperature"
WINDOW_DAYS = 30
TEST_DAYS = 7
SHARE = 0.9
"""The spread is how far off the forecast was this share of the time."""
LEAST = 24

SPREAD_WHY = mark("Nine times in ten over the last %(days)s days, the bias taken off.")


@dataclass(frozen=True, slots=True)
class Row:
    valid: float
    lead: int
    value: float
    observed: float


def biases(rows: Sequence[Row]) -> dict[int, float]:
    out: dict[int, list[float]] = {}
    for r in rows:
        out.setdefault(r.lead, []).append(r.value - r.observed)
    return {lead: sum(v) / len(v) for lead, v in out.items() if len(v) >= LEAST}


def spreads(rows: Sequence[Row], bias: Mapping[int, float]) -> dict[int, float]:
    out: dict[int, list[float]] = {}
    for r in rows:
        out.setdefault(r.lead, []).append(abs(r.value - bias.get(r.lead, 0.0) - r.observed))
    found = {}
    for lead, errors in out.items():
        if len(errors) >= LEAST:
            errors.sort()
            found[lead] = errors[min(len(errors) - 1, math.ceil(SHARE * len(errors)) - 1)]
    return found


def at(by_lead: Mapping[str, float] | Mapping[int, float], hours: float) -> float:
    """A value by lead time at `hours` ahead: in between the lead times by straight lines,
    the nearest one's beyond them."""
    points = sorted((float(k), v) for k, v in by_lead.items())
    if not points:
        return 0.0
    if hours <= points[0][0]:
        return points[0][1]
    for (h0, v0), (h1, v1) in itertools.pairwise(points):
        if hours <= h1:
            return v0 + (v1 - v0) * (hours - h0) / (h1 - h0)
    return points[-1][1]


def judge(rows: Sequence[Row], test_from: float) -> Verdict:
    """The bias from the weeks before `test_from` taken off the forecasts since, against
    the forecasts as given."""
    train = [r for r in rows if r.valid < test_from]
    test = [r for r in rows if r.valid >= test_from]
    bias = biases(train)
    usable = [r for r in test if r.lead in bias]
    return held_out(
        [r.value - bias[r.lead] for r in usable],
        [r.value for r in usable],
        [r.observed for r in usable],
    )


class Forecasts:
    def __init__(self, db: Database, learnings: Learnings) -> None:
        self._db = db
        self._learnings = learnings

    async def run(self, now: float | None = None) -> None:
        now = clock.time() if now is None else now
        found = await self._rows(now - WINDOW_DAYS * 86_400)
        for source, rows in sorted(found.items()):
            scope = f"{source}:{QUANTITY}"
            bias = biases(rows)
            if not bias:
                continue
            verdict = judge(rows, now - TEST_DAYS * 86_400)
            state: State = "proved" if verdict.proved else "learning"
            await self._put(
                BIAS, scope, bias, "model", state, verdict.why, dict(verdict.args), rows
            )
            spread = spreads(rows, bias)
            if spread:
                args: dict[str, float | int | str] = {"days": WINDOW_DAYS}
                await self._put(
                    SPREAD, scope, spread, "observed", "learning", SPREAD_WHY, args, rows
                )

    async def _put(
        self,
        name: str,
        scope: str,
        by_lead: Mapping[int, float],
        kind: str,
        state: State,
        why: str | None,
        args: dict[str, float | int | str],
        rows: Sequence[Row],
    ) -> None:
        await self._learnings.put(
            Learned(
                name=name,
                scope=scope,
                kind="model" if kind == "model" else "observed",
                value={str(lead): round(v, 2) for lead, v in sorted(by_lead.items())},
                unit="K",
                state=state,
                why=why,
                args=args,
                n=len(rows),
                since=min(r.valid for r in rows),
            )
        )

    async def _rows(self, since: float) -> dict[str, list[Row]]:
        def read(t: Transaction) -> dict[str, list[Row]]:
            out: dict[str, list[Row]] = {}
            for source, valid, lead, value, observed in t.execute(
                "SELECT source, valid, lead, value, observed FROM forecast_leads"
                " WHERE quantity = ? AND observed IS NOT NULL AND valid >= ? ORDER BY valid",
                (QUANTITY, since),
            ):
                out.setdefault(source, []).append(Row(valid, lead, value, observed))
            return out

        return await self._db.run(read)


def correction(learnings: Learnings, source: str | None, hours: float) -> float:
    """What to take off a provider's temperature forecast `hours` ahead: its bias there,
    where proved (or pinned); else nothing."""
    if source is None:
        return 0.0
    found = learnings.used(BIAS, f"{source}:{QUANTITY}")
    return at(found, hours) if isinstance(found, dict) else 0.0
