"""From the plan to the levers: what to ask of each now.

**The heating offset** takes whole steps, and every change is a write. Over the next four
hours a small dynamic program picks the steps that come closest to the plan's water,
`w2` °C a step, half a step either side counting as on it, where each change costs a step
held wrong for an hour and a half, with no more changes than what is left of half the
day's write budget, after the changes since midnight, spread over the hours left. Only the
first slot's step is asked for; the next slot plans again. With a room below its band
now, the offset isn't lowered, and with one above it, it isn't raised, whatever the plan
expects.

**A tank** is charged by a boost where the plan charges it in this slot for a hot-water
deadline and the pump isn't charging. For price, its own charges are held off by a block
while the plan charges it no sooner than an hour on, and let go once the plan charges it
within the slot after (only with a cost stance and prices: otherwise the pump's own
thermostat stands). A charge isn't started for price alone: the rooms keep the compressor.
Near its floor every hold is let go, and below it a charge is started, whatever the plan.

**A pool** heats now where the plan heats it now: its start temperature set just above
where it is; else it is left to its band's bottom. **The addition** is kept off where the
plan doesn't use it in the hour, unless the household leaves it to the pump.

**Compressor starts** stay at the pump's own pace: a charge that would start a standing
compressor waits for it to run while today's starts are ahead of the pace the pump kept
on its own, unless a protection floor, a room below its band or a `must` deadline needs
it. And a compressor that stopped within twenty minutes isn't started for a plan's sake.
"""

import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, tzinfo

from ..planner.model import RANK_SLIDER, Decision, LeverState
from .problem import Plan, Problem

AHEAD = 16
"""Slots the offset's steps are planned over."""
WRITE_COST = 3.0
"""What a change of the offset costs, in °C·h of the water's miss: a step held wrong for an
hour and a half."""
DEAD = 0.5
"""How far, in steps, the offset may sit from the plan's before it counts as a miss: the
plan's numbers wander between two steps from one slot to the next, and the offset
shouldn't follow them back and forth."""
RAMP = 1.0
"""The most the offset rises from one slot's step to the next: the plan raises it at most a
step an hour, and the steps never run ahead of it."""
BUDGET_SHARE = 0.5
"""Of what the day's budget leaves for the hours ahead, the share the offset may use."""
CHARGE_SHARE = 0.25
"""The plan charges in a slot where it gives the tank at least this share of it."""
FLOOR_MARGIN = 2.0
SHORT_CYCLE_S = 1200.0
URGENT = 3
"""Ranks up to this start the compressor whatever its pace: floors, comfort's low edges,
`must` deadlines in the default ranking."""


@dataclass(frozen=True)
class TankNow:
    top: float | None
    floor: float | None
    charging: bool


@dataclass(frozen=True)
class Now:
    t: datetime
    zone: tzinfo
    levers: dict[str, LeverState]
    tanks: dict[str, TankNow] = field(default_factory=dict)
    pools: dict[str, float | None] = field(default_factory=dict)
    rooms_below: dict[str, str] = field(default_factory=dict)
    """Per climate system, a room below its band now, by name."""
    rooms_above: dict[str, str] = field(default_factory=dict)
    shifting: bool = False
    """A cost stance and prices: price may move things."""
    budget: tuple[int, int] | None = None
    """Writes in the last day, and the soft budget."""
    compressor_on: bool = False
    stopped_s: float | None = None
    """How long ago the compressor stopped, if it stands."""
    starts_today: int | None = None
    pace: float | None = None
    """How many starts a day the pump made on its own."""
    policy: str | None = None
    """The addition's policy."""
    ranks: dict[str, int] = field(default_factory=dict)
    """comfort_low and comfort_high's places in the ranking."""
    recent: dict[str, int] = field(default_factory=dict)
    """Per lever, the changes made since midnight."""


def _number(value: object, default: float = 0.0) -> float:
    if isinstance(value, int | float) and not isinstance(value, bool):
        return float(value)
    return default


def steps(
    targets: Sequence[float], current: float, lever: LeverState, w2: float, allowed: int
) -> list[float]:
    """The offset's steps over the slots of `targets`, starting from `current`: closest to
    the planned offsets with at most `allowed` changes, each change costing `WRITE_COST`."""
    values = sorted(
        {lever.fit(current)}
        | {lever.fit(t + d) for t in targets for d in (-1.0, -0.5, 0.0, 0.5, 1.0)}
    )
    # By the step it is at and the changes used so far: the least cost, and the path.
    best: dict[tuple[float, int], tuple[float, list[float]]] = {(current, 0): (0.0, [])}
    for target in targets:
        step: dict[tuple[float, int], tuple[float, list[float]]] = {}
        for (was, used), (score, path) in best.items():
            for v in values:
                if v > target + DEAD + 1e-9 or v - was > RAMP + 1e-9:
                    continue  # never ahead of the plan's rise, nor up more than a step
                changes = used + (v != was)
                if changes > allowed:
                    continue
                miss = w2 * max(0.0, abs(v - target) - DEAD) * 0.25
                total = score + miss + (WRITE_COST if v != was else 0.0)
                key = (v, changes)
                if key not in step or total < step[key][0]:
                    step[key] = (total, [*path, v])
        if not step:
            return [current] * len(targets)
        best = step
    return min(best.values(), key=lambda b: b[0])[1]


def decide(plan: Plan, problem: Problem, now: Now) -> list[Decision]:
    """What to ask of each lever now, most important first."""
    out: list[Decision] = []
    out += _heating(plan, problem, now)
    out += _tanks(plan, problem, now)
    out += _pools(plan, problem, now)
    out += _addition(plan, problem, now)
    return sorted(out, key=lambda d: d.rank)


def _allowed(now: Now, lever: str, levers: int) -> int:
    """Changes one setting lever may make over the next four hours: what is left of its
    share of the day's soft budget, after its changes since midnight, spread over the
    hours left in the day; and no more than the budget leaves."""
    if now.budget is None:
        return AHEAD
    used, soft = now.budget
    if soft - used <= 0:
        return 0
    local = now.t.astimezone(now.zone)
    hours = max(1.0, 24 - local.hour - local.minute / 60)
    left = BUDGET_SHARE * soft / max(1, levers) - now.recent.get(lever, 0)
    return max(0, min(soft - used, math.ceil(left * min(1.0, 4 / hours))))


def _price_ahead(problem: Problem) -> tuple[float | None, float | None]:
    """The price now, and the mean over the next six hours."""
    now = problem.slots[0].price if problem.slots else None
    ahead = [s.price for s in problem.slots[1:25] if s.price is not None]
    return now, (sum(ahead) / len(ahead) if ahead else None)


def _heating(plan: Plan, problem: Problem, now: Now) -> list[Decision]:
    out = []
    settings = sum(
        1 for ref in now.levers if ref.endswith(("/heating.offset", "/start_temp", "/stop_temp"))
    )
    for system in problem.systems:
        planned = plan.systems.get(system.scope)
        lever = now.levers.get(f"{system.scope}/heating.offset")
        if planned is None or lever is None or not planned.offset:
            continue
        current = _number(lever.current, system.offset_now)
        targets = [planned.offset[j // 4] for j in range(min(AHEAD, 4 * len(planned.offset)))]
        allowed = _allowed(now, lever.ref, settings)
        if system.scope in now.rooms_below:
            allowed = max(allowed, 1)
        path = steps(targets, current, lever, system.water.step, allowed)
        value = path[0] if path else current
        # Whatever the plan expects, a room outside its band now is never stepped away from.
        if system.scope in now.rooms_below:
            value = max(value, current)
        elif system.scope in now.rooms_above:
            value = min(value, current)
        if value == current:
            continue
        room = planned.room[min(len(planned.room) - 1, 3)]
        price, ahead = _price_ahead(problem)
        if system.scope in now.rooms_below:
            rank = now.ranks.get("comfort_low", 2)
            reason = f"{now.rooms_below[system.scope]} is below its band: more heat"
        elif system.scope in now.rooms_above:
            rank = now.ranks.get("comfort_high", 5)
            reason = f"{now.rooms_above[system.scope]} is above its band: less heat"
        elif value > current:
            rank = RANK_SLIDER
            cheaper = now.shifting and price is not None and ahead is not None and price < ahead
            reason = (
                f"cheaper now than in the hours ahead: storing heat in the house, toward"
                f" {room:.1f} °C"
                if cheaper
                else f"more heat, to keep the rooms near {room:.1f} °C"
            )
        else:
            rank = RANK_SLIDER
            more_expensive = (
                now.shifting and price is not None and ahead is not None and price > ahead
            )
            reason = (
                f"more expensive now than in the hours ahead: using the heat stored in the house,"
                f" letting it drift to {room:.1f} °C"
                if more_expensive
                else f"less heat: the rooms stay near {room:.1f} °C"
            )
        out.append(Decision(lever.ref, "set", {"value": value}, rank, reason))
    return out


def _local(now: Now, t: float) -> str:
    return f"{datetime.fromtimestamp(t, now.zone):%H:%M}"


def _tanks(plan: Plan, problem: Problem, now: Now) -> list[Decision]:
    out = []
    pace = (
        now.pace is not None
        and now.starts_today is not None
        and now.starts_today >= now.pace * _day_share(now) + 1
    )
    for tank in problem.tanks:
        planned = plan.tanks.get(tank.scope)
        state = now.tanks.get(tank.scope)
        block = now.levers.get(f"{tank.scope}/block")
        boost = now.levers.get(f"{tank.scope}/boost_once")
        if planned is None or state is None or (block is None and boost is None):
            continue
        top, floor = state.top, state.floor
        if top is not None and floor is not None and top <= floor + FLOOR_MARGIN:
            why = f"the hot water is {top:.1f} °C, near its floor of {floor:g} °C"
            if block is not None and block.held:
                out.append(Decision(block.ref, "release", rank=1, reason=why))
            if boost is not None and top < floor and not state.charging:
                out.append(Decision(boost.ref, "fire", rank=1, reason=why + ": charging now"))
            continue
        share = planned.share
        upcoming = [d for d in tank.deadlines if d.slot >= 0]
        first = min(upcoming, key=lambda d: d.slot) if upcoming else None
        rank = first.rank if first is not None else RANK_SLIDER
        charge = bool(share) and share[0] >= CHARGE_SHARE
        fired = False
        # A charge is started only for a deadline: for price, the pump's own charges are
        # held off and let go, and the rooms keep the compressor meanwhile.
        if charge and boost is not None and not state.charging and first is not None:
            waits = None
            if not now.compressor_on and rank > URGENT:
                if pace:
                    waits = "today's compressor starts are ahead of the pump's own pace"
                elif now.stopped_s is not None and now.stopped_s < SHORT_CYCLE_S:
                    waits = "the compressor stopped only minutes ago"
            if waits is None:
                end = problem.slots[min(first.slot, len(problem.slots) - 1)].t + 900
                why = f"hot water {first.at_least:g} °C by {_local(now, end)}"
                reason = f"{why}: the plan charges now, the cheapest way to make it"
                if block is not None and block.held:
                    out.append(Decision(block.ref, "release", rank=rank, reason=reason))
                out.append(Decision(boost.ref, "fire", rank=rank, reason=reason))
                fired = True
        if block is None or fired:
            continue
        later = next((j for j, s in enumerate(share) if s >= CHARGE_SHARE), None)
        safe = top is not None and (floor is None or top >= floor + FLOOR_MARGIN)
        if block.held:
            # Held until the plan charges within the next slot.
            want = now.shifting and safe and (later is None or later > 1)
        else:
            # Held only while no charge is planned for the next hour, and not mid-charge.
            want = now.shifting and safe and not state.charging and (later is None or later >= 4)
        if want and not block.held:
            when = (
                f"it charges at {_local(now, problem.slots[later].t)}"
                if later is not None and later < len(problem.slots)
                else "no charge is needed before then"
            )
            reason = f"the plan: the pump's own charges held off; {when}"
            out.append(Decision(block.ref, "engage", rank=RANK_SLIDER, reason=reason))
        elif not want and block.held:
            reason = "the plan charges soon: the pump may charge again"
            out.append(Decision(block.ref, "release", rank=min(rank, RANK_SLIDER), reason=reason))
    return out


def _day_share(now: Now) -> float:
    local = now.t.astimezone(now.zone)
    return (local.hour + local.minute / 60) / 24


def _pools(plan: Plan, problem: Problem, now: Now) -> list[Decision]:
    out: list[Decision] = []
    for pool in problem.pools:
        planned = plan.pools.get(pool.scope)
        start = now.levers.get(f"{pool.scope}/start_temp")
        stop = now.levers.get(f"{pool.scope}/stop_temp")
        block = now.levers.get(f"{pool.scope}/block")
        temp = now.pools.get(pool.scope)
        band = pool.bands[0] if pool.bands else None
        if planned is None or band is None or band.low is None or band.high is None:
            continue
        if not now.shifting:
            for lever in (start, stop, block):
                if lever is not None and _changed(lever):
                    why = "no shifting by price: the pool as the pump has it"
                    out.append(Decision(lever.ref, "restore", rank=RANK_SLIDER, reason=why))
            continue
        heat = bool(planned.share) and planned.share[0] >= CHARGE_SHARE
        if start is not None:
            if heat and temp is not None:
                value = start.fit(min(band.high - 0.5, temp + 0.5))
                reason = "the plan heats the pool now, while it is cheap"
            else:
                value = start.fit(band.low)
                reason = "the pool kept at the bottom of its band until the plan heats it"
            if value != _number(start.current, value + 1):
                out.append(Decision(start.ref, "set", {"value": value}, RANK_SLIDER, reason))
        if stop is not None:
            value = stop.fit(band.high)
            if value != _number(stop.current, value + 1):
                out.append(
                    Decision(stop.ref, "set", {"value": value}, RANK_SLIDER, "its band's top")
                )
        if block is not None:
            soon = any(s >= CHARGE_SHARE for s in planned.share[:2])
            want = not soon and temp is not None and temp >= band.low
            if want and not block.held:
                reason = "the plan heats the pool later: its heating held off"
                out.append(Decision(block.ref, "engage", rank=RANK_SLIDER, reason=reason))
            elif not want and block.held:
                reason = "the plan heats the pool now"
                out.append(Decision(block.ref, "release", rank=RANK_SLIDER, reason=reason))
    return out


def _changed(lever: LeverState) -> bool:
    return lever.held or (
        lever.current is not None
        and lever.baseline is not None
        and _number(lever.current) != _number(lever.baseline)
    )


def _addition(plan: Plan, problem: Problem, now: Now) -> list[Decision]:
    out: list[Decision] = []
    stop = next(
        (lv for ref, lv in sorted(now.levers.items()) if ref.endswith("/addition/stop_temp")), None
    )
    power = next(
        (lv for ref, lv in sorted(now.levers.items()) if ref.endswith("/addition/max_power")), None
    )
    if problem.addition is None:
        return out
    policy = problem.addition.policy
    if power is not None:
        if policy == "limit" and problem.addition.limit_kw is not None:
            value = power.fit(problem.addition.limit_kw)
            why = f"the addition limited to {value:g} kW, as set"
            out.append(Decision(power.ref, "set", {"value": value}, RANK_SLIDER, why))
        elif _changed(power):
            why = "the addition's power as the pump has it"
            out.append(Decision(power.ref, "restore", rank=RANK_SLIDER, reason=why))
    if stop is None:
        return out
    used = sum(max(p.addition_kw[:1] or [0.0]) for p in plan.systems.values())
    cold = bool(now.rooms_below)
    if policy != "pump" and used < 0.05 and not cold and stop.low is not None:
        reason = "the plan needs no addition this hour: kept off"
        if _number(stop.current, stop.low + 1) != stop.low:
            out.append(Decision(stop.ref, "set", {"value": stop.low}, RANK_SLIDER, reason))
    elif _changed(stop):
        reason = (
            "a room is below its band: the addition may help"
            if cold
            else "the plan uses the addition: as the pump has it"
        )
        out.append(Decision(stop.ref, "restore", rank=2 if cold else RANK_SLIDER, reason=reason))
    return out
