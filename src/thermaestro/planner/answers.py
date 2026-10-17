"""The optimizer's answer to a request as it is asked: from a plan made with it, not kept.

- **A deadline:** ready by its time; or about how many minutes late, and how warm the hot
  water would be at its time instead, the two choices there are; or not within two days at
  all. How late is the tank's own model charged from now: a plan stops charging for a
  deadline once it has passed. Where the tank could make it in time and the plan doesn't,
  something ranked above it comes first, and the answer says it is short. Where the
  deadline lies past the published prices, the plan firms up once tomorrow's are out.
- **Comfort:** a room that won't reach its band, how warm it will be instead, and by when
  (past the time its heat takes to show).
- **What gives way for it:** another intent the plan meets now and wouldn't with this one.

Each part is a message marked for translation, with its values.
"""

from collections.abc import Mapping
from datetime import UTC, datetime, tzinfo

from ..core.events import mark
from ..intents import Intent
from ..intents.entry import Said, named, say
from ..learn.cop import carnot
from ..optimize import Problem, Tank
from ..optimize.problem import SLOT_H
from .assurance import AT_RISK_WATER, DEADLINE_TOLERANCE, shorts
from .optimizer import Optimized

READY = mark("The plan has it ready by %(time)s")
LATE = mark("Ready about %(minutes)s minutes late, at %(later)s; or %(temp)s °C at %(time)s")
NOT_WITHIN = mark("It can't be made within two days: at most %(temp)s °C")
FIRMS_UP = mark("The plan firms up when tomorrow's prices are published")
ROOM_SHORT = mark("%(room)s will reach %(temp)s °C, not %(low)s °C, by %(time)s")
GIVES_WAY = mark("%(name_label)s gives way for it")
GIVES_WAY_OFFSET = mark("%(name_label)s (%(offset)s °C) gives way for it")


def answer(
    intent: Intent,
    preview: Optimized,
    current: Optimized | None,
    others: Mapping[str, Intent],
    zone: tzinfo,
) -> list[Said]:
    """What the plan made with `intent` says of it. `current`: the plan without it, the
    last slot's; `others`: the open intents, by id."""
    plan, problem = preview.plan, preview.problem
    if not preview.ok or plan is None or problem is None or not problem.slots:
        return []
    t0 = problem.slots[0].t

    def hhmm(t: float) -> str:
        return datetime.fromtimestamp(t, UTC).astimezone(zone).strftime("%H:%M")

    out: list[Said] = []
    for d in preview.watched.deadlines:
        if d.by != intent.id:
            continue
        planned = plan.tanks.get(d.scope)
        model = next((t for t in problem.tanks if t.scope == d.scope), None)
        if planned is None or model is None or not 0 <= d.slot < len(planned.level):
            continue
        due = t0 + 900 * (d.slot + 1)
        at = planned.level[d.slot]
        if at >= d.at_least - DEADLINE_TOLERANCE:
            out.append(say(READY, time=hhmm(due)))
        else:
            earliest, most = _earliest(model, problem, d.at_least)
            if earliest is None:
                out.append(say(NOT_WITHIN, temp=round(most, 1)))
            elif earliest <= d.slot:
                # It could be made in time: what ranks above it comes first.
                short = say(
                    AT_RISK_WATER, temp=round(at, 1), time=hhmm(due), least=round(d.at_least, 1)
                )
                out.append(short)
            else:
                out.append(
                    say(
                        LATE,
                        minutes=(earliest - d.slot) * 15,
                        later=hhmm(t0 + 900 * (earliest + 1)),
                        temp=round(at, 1),
                        time=hhmm(due),
                    )
                )
        if any(slot.estimated for slot in problem.slots[: d.slot + 1]):
            out.append(say(FIRMS_UP))
        break  # the first deadline it plans for: a recurring one's next
    seen_rooms: set[str] = set()
    for s in shorts(preview.watched, problem, plan):
        if s.intent == intent.id and s.stage == "comfort_low" and s.scope not in seen_rooms:
            seen_rooms.add(s.scope)
            when = hhmm(s.t)
            temp, low = round(s.planned, 1), round(s.wanted, 1)
            out.append(say(ROOM_SHORT, room=s.name, temp=temp, low=low, time=when))
    if current is not None and current.ok and current.plan is not None and current.problem:
        before = {s.intent for s in shorts(current.watched, current.problem, current.plan)}
        after = {s.intent for s in shorts(preview.watched, problem, plan)}
        for id in sorted(after - before - {intent.id}):
            other = others.get(id)
            if other is None:
                continue
            name = named(other)
            out.append(say(GIVES_WAY_OFFSET if "offset" in name else GIVES_WAY, **name))
    return out


def _earliest(tank: Tank, problem: Problem, least: float) -> tuple[int | None, float]:
    """The first slot by whose end the tank could be at `least` °C, charged from now in
    every slot, by its own model (the plan's: its heat per slot, its loss, the draws its
    week expects); and the warmest it gets on the way. A plan stops charging for a
    deadline once it has passed, so how late it can be made is worked out here."""
    level = most = tank.level
    for j, slot in enumerate(problem.slots):
        cop = max(1.0, tank.share * carnot(tank.ceiling, slot.source))
        level += tank.draw_kw * cop * SLOT_H / tank.kwh_per_k
        level -= tank.loss * (level - tank.room_c) * SLOT_H
        level -= tank.draws[j] if j < len(tank.draws) else 0.0
        level = min(level, max(tank.ceiling, tank.level))
        most = max(most, level)
        if level >= least - DEADLINE_TOLERANCE:
            return j, most
    return None, most
