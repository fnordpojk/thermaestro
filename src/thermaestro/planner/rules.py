"""The rules: what to ask of each lever now, and why, from what is in force.

- **Heating with room sensors:** a PI loop on the curve offset per climate system, steering
  the room furthest below its target. The target sits mid-band; with a cost stance it
  moves toward the band's top in cheap hours and its bottom in dear ones, as far as the
  slider allows. A band that rises later is aimed for one response time early; one that
  falls is kept until it does. An old reading counts less, a stale one not at all.
- **Heating without:** the offset raised in the cheapest hours and cut in the dearest, at
  most the household's bound, ranked so it nets out over the window.
- **Hot water:** the floor first (a hold released, a charge started below it); a charge in
  the cheapest window before a deadline the tank won't make by itself, or at once when
  late; the pump's own charges held off in dear hours while the tank is well above its
  floor and no deadline is near.
- **The addition, the pool:** as the household's policy says.
- Between slots, only hot water: the rest changes at most once a slot.
- Quiet hours, a fireplace and a power limit keep the heat from being raised, unless a
  room is below its band or the floor needs it; hands off asks for nothing; without a cost
  stance or prices nothing moves by price; with the write budget spent only ranks 1 to 3
  still change a lever.

The rules keep their loop state in `Situation.memory`; otherwise they only read.
"""

import math
import statistics
from collections.abc import Iterable
from datetime import datetime, timedelta

from ..intents.entry import RESPONSE_H
from .model import RANK_SLIDER, SLOT, Decision, LeverState, Memory, Price, RoomReading, Situation

WINDOW = timedelta(hours=12)
"""How far back and ahead prices are compared, at least."""
DEAR, STILL_DEAR = -0.3, -0.1
"""The price signal below which an hour is dear, and the one it must rise above again
before a hold for price is released."""
FLOOR_MARGIN = 2.0
"""How close to its floor the tank's top may come before every hold on it is released."""
HOLD_MARGIN = 5.0
"""How far above its floor the tank's top must be for price to hold its charges off: a
shower can take several degrees off the top in minutes, and when the household draws
water isn't learned yet."""
DEADLINE_MARGIN = 1.0
TANK_LOSS_C_H = 0.5
"""How fast the tank's top is assumed to cool, until it is learned."""
CHARGE_S = 3600.0
"""How long a charge is assumed to take until one has been seen."""
KP = {
    "radiators": 1.5,
    "fan_coils": 2.0,
    "floor_light": 1.25,
    "slab": 0.75,
    "radiators_and_floor_light": 1.25,
    "radiators_and_slab": 1.0,
    "unknown": 1.0,
}
"""Offset steps per °C a room is off its target, by emitter: a slow slab gets less."""
EDGE_MARGIN = 0.25
"""How far inside the band price may move a room's target: never to the edge itself, but
near it, so that a band of 2 °C leaves price ±0.75 °C."""
DEADBAND = 0.75
"""How far, in steps, the loop must want the offset from where it is before it changes it
within the band: less would flip it back and forth."""
WEAR_RANK = 3
"""With the day's write budget spent, only decisions up to this rank are sent."""
BOOST_KW = 2.0
BLOCK_MOST = timedelta(hours=3)
"""The longest price holds the hot water's charges off: the charge that follows takes the
heat from the rooms for a while, and a long hold makes it longer."""
"""What a charge adds to the house's power, until it is learned."""


def plan(sit: Situation) -> list[Decision]:
    """Everything the planner asks for now, most important first."""
    if sit.force.hands_off_until is not None and sit.now < sit.force.hands_off_until:
        return []
    if sit.slot:
        decisions = [*heating(sit), *hot_water(sit), *addition(sit), *pools(sit)]
    else:
        decisions = hot_water(sit)
    if sit.budget is not None and sit.budget[0] >= sit.budget[1]:
        decisions = [d for d in decisions if d.rank <= WEAR_RANK]
    return sorted(decisions, key=lambda d: d.rank)


# --- prices -------------------------------------------------------------------------------


def price_at(prices: Iterable[Price], t: datetime) -> float | None:
    for p in prices:
        if p.start <= t < p.start + SLOT:
            return p.value
    return None


def _window(prices: list[Price], now: datetime, half: timedelta) -> list[float]:
    return [p.value for p in prices if now - half <= p.start < now + half]


def signal(prices: list[Price], now: datetime, half: timedelta = WINDOW) -> float | None:
    """How cheap the price now is against the hours around it: 1 the cheapest, -1 the
    dearest, 0 the median. None without enough prices."""
    current = price_at(prices, now)
    window = _window(prices, now, half)
    if current is None or len(window) < 8:
        return None
    low, high = min(window), max(window)
    median = statistics.median(window)
    if high - low < max(1e-3, 0.02 * abs(median)):
        return 0.0
    return max(-1.0, min(1.0, (median - current) / ((high - low) / 2)))


def rank_signal(prices: list[Price], now: datetime, half: timedelta = WINDOW) -> float | None:
    """The price now by its rank among the hours around it, 1 the cheapest to -1 the
    dearest: a shift by it nets out over the window."""
    current = price_at(prices, now)
    window = _window(prices, now, half)
    if current is None or len(window) < 8:
        return None
    below = sum(1 for v in window if v < current)
    same = sum(1 for v in window if v == current)
    rank = below + (same - 1) / 2
    return 1.0 - 2.0 * rank / (len(window) - 1)


def cheapest_start(
    prices: list[Price], now: datetime, deadline: datetime, length_s: float
) -> datetime | None:
    """The slot to start a charge of `length_s` in so it ends by `deadline` at the least
    mean price; None if no window has prices."""
    first = now - timedelta(seconds=now.timestamp() % SLOT.total_seconds())
    slots = max(1, round(length_s / SLOT.total_seconds()))
    best: tuple[float, datetime] | None = None
    start = first
    while start + slots * SLOT <= deadline:
        values = [price_at(prices, start + i * SLOT) for i in range(slots)]
        if all(v is not None for v in values):
            mean = sum(v for v in values if v is not None) / slots
            if best is None or mean < best[0]:
                best = (mean, start)
        start += SLOT
    return None if best is None else best[1]


# --- helpers ------------------------------------------------------------------------------


def _number(value: object, default: float = 0.0) -> float:
    if isinstance(value, int | float) and not isinstance(value, bool):
        return float(value)
    return default


def _changed(lever: LeverState) -> bool:
    return lever.held or (
        lever.current is not None
        and lever.baseline is not None
        and _number(lever.current) != _number(lever.baseline)
    )


def _quiet(sit: Situation) -> bool:
    bound = sit.force.bound("quiet", "house")
    return bound is not None and bound.value is True


def power_limit(sit: Situation) -> float | None:
    """The most the house may draw now: the household's limit, or the grid company's
    where that is lower."""
    bound = sit.force.bound("house_power", "house")
    limits = [bound.high] if bound is not None and bound.high is not None else []
    if sit.grid_limit is not None:
        limits.append(sit.grid_limit.kw)
    return min(limits, default=None)


def _over_peak(sit: Situation, extra_kw: float = 0.0) -> bool:
    limit = power_limit(sit)
    return limit is not None and sit.house_kw is not None and sit.house_kw + extra_kw > limit


def _local(sit: Situation, t: datetime) -> str:
    return f"{t.astimezone(sit.zone):%H:%M}"


def _name(scope: str) -> str:
    return scope.removeprefix("room:").replace("-", " ").replace("_", " ")


def _restore(lever: LeverState, reason: str, rank: int = RANK_SLIDER) -> list[Decision]:
    return [Decision(lever.ref, "restore", rank=rank, reason=reason)] if _changed(lever) else []


# --- heating ------------------------------------------------------------------------------


def heating(sit: Situation) -> list[Decision]:
    out: list[Decision] = []
    for system in sorted(sit.caps.systems):
        lever = sit.levers.get(f"{system}/heating.offset")
        if lever is None:
            continue
        if not sit.heating:
            out += _restore(lever, "the pump's heating stop has heating off: its own offset")
            continue
        rooms = [r for r in sit.rooms if r.system == system]
        found = _steer(sit, system, lever, rooms) if rooms else _shift(sit, system, lever)
        out += found
    return out


def _band(sit: Situation, room: RoomReading) -> tuple[float, float] | None:
    """A room's band in force: its own, or its climate system's."""
    bound = sit.force.bound("room_temp", room.scope) or sit.force.bound("room_temp", room.system)
    if bound is None or bound.low is None or bound.high is None:
        return None
    return bound.low, bound.high


def _response_h(sit: Situation, system: str) -> float:
    emitter = sit.caps.emitters.get(system, "unknown")
    return RESPONSE_H.get(emitter, RESPONSE_H["unknown"])


def _steer(
    sit: Situation, system: str, lever: LeverState, rooms: list[RoomReading]
) -> list[Decision]:
    live = [r for r in rooms if r.weight > 0]
    if not live:
        return _restore(
            lever, "no room in it has a fresh temperature: the pump's own offset meanwhile", 2
        )
    response = _response_h(sit, system)
    half = max(WINDOW, timedelta(hours=response))
    s = signal(sit.prices, sit.now, half)
    lean = sit.force.slider * s if sit.force.slider is not None and s is not None else 0.0
    worst: tuple[float, RoomReading, float, float, float, float] | None = None
    for room in live:
        if room.airing:
            continue  # not chased: it comes back once the window is shut
        band = _band(sit, room)
        if band is None or room.temp is None:
            continue
        low, high = band
        ahead_low, ahead_high = sit.ahead.get(room.scope, (None, None))
        low = max(low, ahead_low) if ahead_low is not None else low
        high = max(high, ahead_high) if ahead_high is not None else high
        reach = max(0.0, (high - low) / 2 - EDGE_MARGIN)
        target = (low + high) / 2 + reach * lean
        error = target - room.temp
        if worst is None or error > worst[0]:
            worst = (error, room, room.temp, low, high, target)
    if worst is None:
        return []  # nothing to steer by: as it is
    error, room, temp, low, high, target = worst
    memory: Memory = sit.memory
    dt = 0.0
    if memory.last_round is not None:
        dt = min(1800.0, max(0.0, (sit.now - memory.last_round).total_seconds()))
    emitter = sit.caps.emitters.get(system, "unknown")
    kp = KP.get(emitter, KP["unknown"])
    base = _number(lever.baseline, _number(lever.current))
    step = kp * error * dt / (2 * response * 3600) * room.weight
    integral = memory.integral.get(system, 0.0)
    # Outside the band the integral stops pushing the wrong way, so it doesn't hold the
    # room there.
    if temp < low:
        integral = max(integral, 0.0)
    elif temp > high:
        integral = min(integral, 0.0)
    integral += step
    lowest = (lever.low if lever.low is not None else -10.0) - base
    highest = (lever.high if lever.high is not None else 10.0) - base
    integral = max(lowest, min(highest, integral))
    memory.integral[system] = integral
    raw = base + kp * error * room.weight + integral
    value = lever.fit(raw)
    current = _number(lever.current, base)
    below = temp < low
    if low <= temp <= high and abs(raw - current) < DEADBAND * (lever.step or 1.0):
        value = current
    held_back = None
    if value > current and not below:
        if system in sit.force.fireplace or "house" in sit.force.fireplace:
            held_back = "a fireplace is on"
        elif _quiet(sit):
            held_back = "quiet hours"
        elif _over_peak(sit):
            held_back = "the house is near its power limit"
    if held_back is not None:
        value = current
    name = room.name or _name(room.scope)
    if below:
        rank = sit.force.rank("comfort_low")
        shown = math.floor(temp * 10) / 10  # never shown as at the edge it is below
        reason = f"{name} is {shown:.1f} °C, below its {low:g} °C: more heat"
    elif temp > high:
        rank = sit.force.rank("comfort_high")
        shown = math.ceil(temp * 10) / 10
        reason = f"{name} is {shown:.1f} °C, above its {high:g} °C: less heat"
    elif lean > 0.25:
        rank = RANK_SLIDER
        reason = f"cheap hours: warming {name} toward {target:.1f} °C, high in its band"
    elif lean < -0.25:
        rank = RANK_SLIDER
        reason = f"dear hours: letting {name} drift to {target:.1f} °C, low in its band"
    else:
        rank = RANK_SLIDER
        reason = f"keeping {name} near {target:.1f} °C"
    if held_back is not None:
        reason += f"; not raised: {held_back}"
    return [Decision(lever.ref, "set", {"value": value}, rank, reason)]


def _shift(sit: Situation, system: str, lever: LeverState) -> list[Decision]:
    """Without a room sensor: the offset moved by price, within the household's bound."""
    bound = sit.force.bound("offset_shift", system)
    if bound is None or bound.low is None or bound.high is None:
        return []
    half = max(WINDOW, timedelta(hours=_response_h(sit, system)))
    s = rank_signal(sit.prices, sit.now, half)
    if sit.force.slider is None or s is None:
        why = "no cost stance" if sit.force.slider is None else "no prices"
        return _restore(lever, f"{why}: the pump's own offset")
    steps = min(abs(bound.low), abs(bound.high))
    shift = round(steps * sit.force.slider * s)
    if _quiet(sit) or _over_peak(sit):
        shift = min(shift, 0)
    base = _number(lever.baseline, _number(lever.current))
    value = lever.fit(base + shift)
    if shift > 0:
        reason = f"cheap hour: the heat raised {shift:+d} ahead of dearer ones"
    elif shift < 0:
        reason = f"dear hour: the heat lowered {shift:+d}, made up in cheaper ones"
    else:
        reason = "an average hour: normal heat"
    return [Decision(lever.ref, "set", {"value": value}, RANK_SLIDER, reason)]


# --- hot water ----------------------------------------------------------------------------


def next_periodic(memory: Memory, scope: str) -> datetime | None:
    """When the pump's periodic increase is due, from the last two seen."""
    seen = memory.periodic.get(scope)
    before = memory.periodic.get(f"{scope}#before")
    if seen is None or before is None or seen <= before:
        return None
    return seen + (seen - before)


def hot_water(sit: Situation) -> list[Decision]:
    out: list[Decision] = []
    charging = sit.demand == "dhw"
    for scope, tank in sorted(sit.tanks.items()):
        block = sit.levers.get(f"{scope}/block")
        boost = sit.levers.get(f"{scope}/boost_once")
        if block is None and boost is None:
            continue
        top = tank.top
        floor = sit.force.bound("tank_top_temp", scope)
        least = floor.low if floor is not None else None
        if top is not None and least is not None and top <= least + FLOOR_MARGIN:
            sit.memory.blocked[scope] = False
            why = f"the hot water is {top:.1f} °C, near its floor of {least:g} °C"
            if block is not None and block.held:
                out.append(Decision(block.ref, "release", rank=1, reason=why))
            if boost is not None and top < least and not charging:
                out.append(Decision(boost.ref, "fire", rank=1, reason=why + ": charging now"))
            continue
        charge_s = sit.memory.charge_s.get(scope, CHARGE_S)
        periodic = next_periodic(sit.memory, scope)
        need = None
        for deadline in sit.deadlines:
            if deadline.scope not in (scope, "house") or deadline.t <= sit.now:
                continue
            if periodic is not None and sit.now < periodic <= deadline.t:
                continue  # the pump's periodic increase heats it before then anyway
            hours = (deadline.t - sit.now).total_seconds() / 3600
            expected = None if top is None else top - TANK_LOSS_C_H * hours
            if expected is None or expected < deadline.at_least + DEADLINE_MARGIN:
                need = deadline
                break
        boosting: int | None = None  # the rank of a charge asked for now
        if boost is not None and not charging:
            if scope in sit.boost_scopes:
                rank = boosting = sit.force.rank("must_deadlines")
                out.append(Decision(boost.ref, "fire", rank=rank, reason="boost now, as asked"))
            elif need is not None:
                left = (need.t - sit.now).total_seconds()
                late = left <= charge_s + SLOT.total_seconds()
                start = cheapest_start(sit.prices, sit.now, need.t, charge_s)
                window = start is not None and start <= sit.now < start + SLOT
                if start is None and sit.prices == []:
                    window = left <= 2 * charge_s  # no prices: in good time before it
                must = need.strength == "must"
                hindered = _quiet(sit) or _over_peak(sit, BOOST_KW)
                if (late or window) and (must or not hindered):
                    boosting = need.rank
                    when = _local(sit, need.t)
                    how = "starting now to make it" if late else "the cheapest time before it"
                    reason = f"hot water {need.at_least:g} °C by {when}: {how}"
                    out.append(Decision(boost.ref, "fire", rank=need.rank, reason=reason))
        if block is None:
            continue
        s = signal(sit.prices, sit.now)
        was = sit.memory.blocked.get(scope, False)
        dear = sit.force.slider is not None and s is not None and s < (STILL_DEAR if was else DEAR)
        safe = top is not None and (least is None or top >= least + HOLD_MARGIN)
        near = need is not None and (need.t - sit.now).total_seconds() <= 2 * charge_s
        since = sit.memory.blocked_since.get(scope)
        long = since is not None and sit.now - since >= BLOCK_MOST
        want = dear and safe and not near and boosting is None and not long
        sit.memory.blocked[scope] = want
        if want and since is None:
            sit.memory.blocked_since[scope] = sit.now
        elif not want and not dear:
            sit.memory.blocked_since.pop(scope, None)  # a new hold may begin
        if want and not block.held:
            reason = "dear hours: the hot water's charges held off"
            out.append(Decision(block.ref, "engage", rank=RANK_SLIDER, reason=reason))
        elif not want and block.held:
            why = (
                "a deadline is near"
                if near or boosting is not None
                else "held long enough"
                if long
                else "cheaper now"
            )
            reason = f"{why}: the pump may charge again"
            # Released before a charge asked for it is fired: ranked with it.
            rank = boosting if boosting is not None else RANK_SLIDER
            out.append(Decision(block.ref, "release", rank=rank, reason=reason))
    return out


# --- the addition and the pools -----------------------------------------------------------


def _find(sit: Situation, suffix: str) -> LeverState | None:
    return next((lv for ref, lv in sorted(sit.levers.items()) if ref.endswith(suffix)), None)


def addition(sit: Situation) -> list[Decision]:
    stop = _find(sit, "/addition/stop_temp")
    power = _find(sit, "/addition/max_power")
    bound = sit.force.bound("addition", "house")
    policy = bound.value if bound is not None else None
    out: list[Decision] = []
    cold = any(
        r.temp is not None and (band := _band(sit, r)) is not None and r.temp < band[0]
        for r in sit.rooms
    )
    if power is not None:
        if policy == "limit" and bound is not None and bound.high is not None:
            value = power.fit(bound.high)
            why = f"the addition limited to {value:g} kW, as set"
            out.append(Decision(power.ref, "set", {"value": value}, RANK_SLIDER, why))
        else:
            out += _restore(power, "the addition's power as the pump has it")
    if stop is not None:
        s = signal(sit.prices, sit.now)
        dear = policy == "not_when_expensive" and s is not None and s < DEAR
        peak = _over_peak(sit)
        if (dear or peak) and not cold and stop.low is not None:
            why = "dear hours" if dear else "the house is near its power limit"
            reason = f"{why}: the addition kept off"
            out.append(Decision(stop.ref, "set", {"value": stop.low}, RANK_SLIDER, reason))
        else:
            reason = (
                "a room is below its band: the addition may help"
                if cold
                else ("the addition as the pump has it")
            )
            out += _restore(stop, reason, 2 if cold else RANK_SLIDER)
    return out


def pools(sit: Situation) -> list[Decision]:
    out: list[Decision] = []
    for scope, temp in sorted(sit.pools.items()):
        start = sit.levers.get(f"{scope}/start_temp")
        stop = sit.levers.get(f"{scope}/stop_temp")
        block = sit.levers.get(f"{scope}/block")
        bound = sit.force.bound("pool_temp", scope)
        s = signal(sit.prices, sit.now)
        if bound is None or bound.low is None or bound.high is None:
            continue
        if sit.force.slider is None or s is None:
            for lever in (start, stop, block):
                if lever is not None:
                    out += _restore(lever, "no shifting by price: the pool as the pump has it")
            continue
        low, high = bound.low, bound.high
        lean = sit.force.slider * max(s, 0.0)
        if _quiet(sit) or _over_peak(sit):
            lean = 0.0
        if start is not None:
            value = start.fit(low + (high - low - 0.5) * lean)
            reason = (
                "cheap hours: the pool warmed toward its top"
                if lean > 0
                else ("the pool kept at the bottom of its band")
            )
            out.append(Decision(start.ref, "set", {"value": value}, RANK_SLIDER, reason))
        if stop is not None:
            value = stop.fit(high)
            out.append(Decision(stop.ref, "set", {"value": value}, RANK_SLIDER, "its band's top"))
        if block is not None:
            want = s < -0.5 and temp is not None and temp >= low
            if want and not block.held:
                reason = "the dearest hours: the pool's heating held off"
                out.append(Decision(block.ref, "engage", rank=RANK_SLIDER, reason=reason))
            elif not want and block.held:
                reason = "the pool may heat again"
                out.append(Decision(block.ref, "release", rank=RANK_SLIDER, reason=reason))
    return out
