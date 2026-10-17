"""How each intent the optimizer plans stands in its plan.

- **At risk:** the plan expects a miss beyond what is tolerated: a room below its band for
  an hour or more, once its heat has had time to show (a band can't be met sooner than the
  house answers), or the hot water short at a deadline.
- **Giving way:** the miss is a higher goal's doing. Told apart by solving once more with
  the short goal's stage ranked first: if the plan then meets it, it was giving way, to the
  stages above it that came out worse.
- Otherwise it is planned to be met, and stays (or goes back to) active.

Only the next day is judged: further ahead, the forecast and the prices are guesses. Each
finding is a message marked for translation, with its values.
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, tzinfo
from typing import Any

from ..core.events import mark
from ..intents.model import State
from ..optimize import Plan, Problem
from . import rules
from .optimizer import Watched

AHEAD_H = 24
"""How far ahead the plan is judged."""
COMFORT_TOLERANCE = 0.1
"""°C a room may be planned below its band before it counts."""
DEADLINE_TOLERANCE = 0.3
"""°C the hot water may be planned short of a deadline before it counts."""
STAGE_TOLERANCE = 0.01
"""How much worse, in its own unit, a stage must come out to have been given way to."""
STAGE = {"must": "must_deadlines", "should": "should_deadlines"}

AT_RISK_ROOM = mark("The plan has %(room)s at %(temp)s °C at %(time)s, below its %(low)s °C")
AT_RISK_WATER = mark("The plan has the hot water at %(temp)s °C at %(time)s, short of %(least)s °C")
GIVING_WAY_ROOM = mark(
    "Gives way to %(what_label)s: the plan has %(room)s at %(temp)s °C at %(time)s,"
    " below its %(low)s °C"
)
GIVING_WAY_WATER = mark(
    "Gives way to %(what_label)s: the plan has the hot water at %(temp)s °C at %(time)s,"
    " short of %(least)s °C"
)
GIVEN_WAY_TO = {
    "floors": mark("a protection floor"),
    "comfort_low": mark("comfort"),
    "must_deadlines": mark("hot water that must be ready"),
    "should_deadlines": mark("hot water that should be ready"),
    "comfort_high": mark("not overheating"),
    "power_peak": mark("the power limit"),
}


@dataclass(frozen=True)
class Short:
    """Where the plan misses what an intent asks."""

    intent: str
    stage: str
    scope: str
    name: str
    t: float
    """When: the end of the first hour short, or the deadline."""
    planned: float
    wanted: float


@dataclass(frozen=True)
class Finding:
    state: State
    """at_risk or giving_way."""
    message: str
    args: dict[str, Any] = field(default_factory=dict)

    def text(self) -> str:
        return self.message % self.args


def shorts(watched: Watched, problem: Problem, plan: Plan) -> list[Short]:
    """Each intent's first miss in the plan: rooms by the hour, deadlines at their slot."""
    t0 = problem.slots[0].t if problem.slots else 0.0
    systems = {s.scope: s for s in problem.systems}
    found: dict[tuple[str, str], Short] = {}
    for scope, goal in watched.rooms.items():
        system, planned = systems.get(goal.system), plan.systems.get(goal.system)
        if system is None or planned is None:
            continue
        off = next((r.off for r in system.rooms if r.scope == scope), 0.0)
        rose_at, before = 0, None
        for h in range(min(AHEAD_H, len(planned.room), len(goal.low))):
            low = goal.low[h]
            if low is not None and before is not None and low > before + 1e-9:
                rose_at = h
            before = low
            if low is None or h + 1 - rose_at < goal.reach_h:
                continue
            temp = planned.room[h] + off
            if temp >= low - COMFORT_TOLERANCE:
                continue
            for intent in goal.by[h]:
                key = (intent, scope)
                if key not in found:
                    t = t0 + 3600 * (h + 1)
                    name = goal.name or rules._name(scope)
                    found[key] = Short(intent, "comfort_low", scope, name, t, temp, low)
    for d in watched.deadlines:
        tank = plan.tanks.get(d.scope)
        if tank is None or not 0 <= d.slot < len(tank.level):
            continue
        if d.slot >= 4 * AHEAD_H:
            continue
        level = tank.level[d.slot]
        if level < d.at_least - DEADLINE_TOLERANCE:
            t = t0 + 900 * (d.slot + 1)
            stage = STAGE.get(d.strength, "should_deadlines")
            short = Short(d.by, stage, d.scope, d.scope, t, level, d.at_least)
            found.setdefault((d.by, d.scope), short)
    return sorted(found.values(), key=lambda s: (s.t, s.intent))


def judge(
    watched: Watched,
    problem: Problem,
    plan: Plan,
    zone: tzinfo,
    solve: Callable[[Problem], Plan],
) -> dict[str, Finding]:
    """How each intent with a miss in the plan stands: at risk, or giving way. An intent
    not in the result is planned to be met. Runs off the event loop: it may solve again."""
    found = shorts(watched, problem, plan)
    if not found:
        return {}
    ranking = list(problem.ranking)
    gave: dict[str, str] = {}
    for stage in sorted({s.stage for s in found}, key=lambda s: _place(ranking, s)):
        above = ranking[: _place(ranking, stage)]
        if not above:
            continue
        first = (stage, *[r for r in ranking if r != stage])
        other = solve(replace(problem, ranking=first))
        if not other.ok:
            continue
        still = {s.intent for s in shorts(watched, problem, other) if s.stage == stage}
        worse = [
            a for a in above if other.stages.get(a, 0.0) > plan.stages.get(a, 0.0) + STAGE_TOLERANCE
        ]
        for s in found:
            if s.stage == stage and s.intent not in still:
                gave[s.intent] = (worse or above)[0]
    out: dict[str, Finding] = {}
    for s in found:
        if s.intent in out:
            continue
        when = datetime.fromtimestamp(s.t, UTC).astimezone(zone).strftime("%H:%M")
        if s.stage == "comfort_low":
            args: dict[str, Any] = {
                "room": s.name,
                "temp": round(s.planned, 1),
                "time": when,
                "low": round(s.wanted, 1),
            }
            messages = (AT_RISK_ROOM, GIVING_WAY_ROOM)
        else:
            args = {"temp": round(s.planned, 1), "time": when, "least": round(s.wanted, 1)}
            messages = (AT_RISK_WATER, GIVING_WAY_WATER)
        if s.intent in gave:
            args["what_label"] = GIVEN_WAY_TO.get(gave[s.intent], gave[s.intent])
            out[s.intent] = Finding("giving_way", messages[1], args)
        else:
            out[s.intent] = Finding("at_risk", messages[0], args)
    return out


def _place(ranking: Sequence[str], stage: str) -> int:
    return ranking.index(stage) if stage in ranking else len(ranking)
