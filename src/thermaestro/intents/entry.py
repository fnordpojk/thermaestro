"""The check every new or changed intent gets before it is kept.

- Its end is made definite: a request over MQTT is temporary and ends at the next change
  if nothing else; "hands off" lasts at most 48 hours; an end already past is refused.
- What it names must exist: its levels, for its kind of scope.
- A contradiction (a floor above a ceiling, from intents of one tier) is refused here; it
  never reaches the planner.
- What the house can't do is said, with what happens instead: no room sensor, no
  hot-water block, no power reading.
- The answer says when it ends, what it shadows, and on a slow heating system, how long
  the change takes to show.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from ..store import Emitter
from .calendar import Calendar
from .kinds import HANDS_OFF_MOST
from .model import STANDING, TEMPORARY, Intent, Level, Validity
from .resolve import STEP, Resolver

CHECK_SPAN = timedelta(days=7)
"""How far ahead a contradiction is looked for: a week covers every weekly pattern."""
RESPONSE_H: dict[str, float] = {
    "radiators": 2.0,
    "fan_coils": 1.0,
    "floor_light": 3.0,
    "slab": 12.0,
    "radiators_and_floor_light": 3.0,
    "radiators_and_slab": 6.0,
    "unknown": 4.0,
}
"""How long a change of heat takes to show in the room, by emitter, until it is learned."""
EMITTER_WORDS = {
    "radiators": "the radiators",
    "fan_coils": "the fan coils",
    "floor_light": "the floor heating",
    "slab": "the floor heating",
    "radiators_and_floor_light": "the heating",
    "radiators_and_slab": "the heating",
    "unknown": "the heating",
}


@dataclass(frozen=True)
class Capabilities:
    """What the house has that intents rest on."""

    systems: frozenset[str] = frozenset()
    """Climate systems' scopes."""
    rooms: Mapping[str, str] = field(default_factory=dict)
    """Room scopes with a temperature sensor, to their climate system's scope."""
    offset: frozenset[str] = frozenset()
    """Climate systems whose heating offset can be changed."""
    tanks: frozenset[str] = frozenset()
    block: frozenset[str] = frozenset()
    """Tanks whose charges can be held off."""
    pools: frozenset[str] = frozenset()
    addition: bool = False
    power: bool = False
    """A whole-house power reading."""
    prices: bool = False
    emitters: Mapping[str, Emitter] = field(default_factory=dict)


@dataclass(frozen=True)
class Verdict:
    intent: Intent
    accepted: bool
    messages: tuple[str, ...] = ()


def check(
    intent: Intent,
    others: list[Intent],
    levels: Mapping[str, Level],
    caps: Capabilities,
    calendar: Calendar,
    now: datetime,
) -> Verdict:
    messages: list[str] = []

    def refuse(why: str) -> Verdict:
        return Verdict(
            intent.model_copy(update={"state": "rejected", "why": why}), False, (why, *messages)
        )

    if intent.principal == "mqtt" and intent.kind in STANDING:
        return refuse("a request over MQTT is for a while: standing intents are set in the UI")
    if intent.kind == "power_peak" and not caps.power:
        return refuse("a power limit needs a whole-house power reading; there is none yet")
    intent, clipped = _definite_end(intent)
    if clipped:
        messages.append(clipped)
    end = Resolver([intent], levels, calendar).end(intent)
    if end is not None and end <= now:
        return refuse("it would already have ended")
    missing = _missing_levels(intent, levels)
    if missing:
        return refuse(missing)
    contradiction = _contradiction(intent, others, levels, calendar, now)
    if contradiction:
        return refuse(contradiction)
    messages.extend(_gaps(intent, caps))
    resolver = Resolver([*others, intent], levels, calendar, dict(caps.rooms))
    at = max(intent.validity.starts or now, now)
    before = Resolver(others, levels, calendar, dict(caps.rooms)).in_force(at).paused
    after = resolver.in_force(at).paused
    for other in others:
        if other.id in after and other.id not in before:
            messages.append(f"{_name(other)} ({other.principal}) is set aside meanwhile")
    if intent.kind == "warmer":
        messages.extend(_reach(intent, caps))
    messages.insert(0, _until(intent, resolver, calendar))
    state = "scheduled" if intent.validity.starts and intent.validity.starts > now else "active"
    accepted = intent.model_copy(update={"state": state, "why": None})
    return Verdict(accepted, True, tuple(m for m in messages if m))


def _definite_end(intent: Intent) -> tuple[Intent, str | None]:
    v = intent.validity
    if intent.kind == "hands_off":
        start = v.starts or intent.created
        latest = start + HANDS_OFF_MOST
        if v.at is None or v.at > latest:
            ended = Validity(starts=v.starts, ends="at", at=latest)
            return intent.model_copy(update={"validity": ended}), (
                "hands off lasts at most 48 hours"
            )
    return intent, None


def _missing_levels(intent: Intent, levels: Mapping[str, Level]) -> str | None:
    named = [t.level for e in intent.expectations for t in e.targets if t.level]
    for key in ("hot_water",):
        value = intent.parameters.get(key)
        if isinstance(value, str):
            named.append(value)
    chosen = intent.parameters.get("levels")
    if isinstance(chosen, dict):
        named.extend(str(v) for v in chosen.values())
    for level_id in named:
        if level_id not in levels:
            return f"there is no level {level_id!r}"
    return None


def _contradiction(
    intent: Intent,
    others: list[Intent],
    levels: Mapping[str, Level],
    calendar: Calendar,
    now: datetime,
) -> str | None:
    """A floor above a ceiling from intents of the intent's own tier, within a week."""
    same = [o for o in others if o.open and o.tier == intent.tier and o.id != intent.id]
    if intent.kind in TEMPORARY or not intent.expectations:
        return None
    resolver = Resolver([*same, intent], levels, calendar)
    t = max(now, intent.validity.starts or now)
    stop = t + CHECK_SPAN
    while t < stop:
        for (target, scope), bound in resolver.in_force(t).bounds.items():
            if bound.low is not None and bound.high is not None and bound.low > bound.high:
                at = calendar.local(t)
                return (
                    f"it contradicts what is set for {scope}: {target.replace('_', ' ')} at"
                    f" least {bound.low:g} and at most {bound.high:g}, {at:%A %H:%M}"
                )
        t += STEP
    return None


def _gaps(intent: Intent, caps: Capabilities) -> list[str]:
    """What the house lacks for the intent, and what happens instead."""
    out = []
    systems = caps.systems if intent.scope == "house" else {intent.scope} & caps.systems
    if intent.kind in ("comfort_band", "warmer"):
        for system in sorted(systems):
            if system not in caps.offset:
                out.append(f"{system}'s heating can't be changed: shown, not acted on")
            elif not any(s == system for s in caps.rooms.values()) and not intent.parameters.get(
                "no_sensor"
            ):
                out.append(
                    f"{system} has no room sensor: normal heat, shifted by price within a"
                    " bound instead of a band"
                )
    if intent.kind in ("hot_water_by", "bath") and intent.scope not in caps.block:
        out.append("without a hot-water block, charges can only be added when cheap, not held off")
    if intent.kind == "cost_stance" and not caps.prices:
        out.append("no prices yet: comfort only, no shifting by price")
    if intent.kind == "addition_policy" and not caps.addition:
        out.append("the addition can't be changed here: the pump's own logic stays")
    if intent.kind == "pool" and intent.scope not in caps.pools:
        out.append("the pool can't be changed here: the pump's own pool control stays")
    return out


def _reach(intent: Intent, caps: Capabilities) -> list[str]:
    """On a slow system, how long a warmer or cooler takes to show."""
    offset = intent.parameters.get("offset")
    if not isinstance(offset, int | float):
        return []
    systems = sorted(caps.systems if intent.scope == "house" else {intent.scope})
    out = []
    for system in systems:
        emitter = caps.emitters.get(system, "unknown")
        hours = RESPONSE_H.get(emitter, RESPONSE_H["unknown"])
        if hours >= 4:
            way = "warmer" if offset > 0 else "cooler"
            out.append(
                f"{EMITTER_WORDS.get(emitter, 'the heating')} will be {abs(offset):g} °C {way}"
                f" in about {hours:g} hours"
            )
    return out


def _until(intent: Intent, resolver: Resolver, calendar: Calendar) -> str:
    end = resolver.end(intent)
    if end is None:
        return ""
    when = calendar.local(end)
    if intent.validity.ends == "when_met":
        return f"{_name(intent)}: until done, at the latest {when:%Y-%m-%d %H:%M}"
    if intent.validity.ends == "next_change":
        return f"{_name(intent)}: until the next change, {when:%Y-%m-%d %H:%M}"
    return f"{_name(intent)}: until {when:%Y-%m-%d %H:%M}"


def _name(intent: Intent) -> str:
    if intent.kind == "warmer":
        offset = intent.parameters.get("offset")
        if isinstance(offset, int | float) and offset < 0:
            return f"Cooler, please ({offset:+g} °C)"
        return f"Warmer, please ({offset:+g} °C)" if isinstance(offset, int | float) else "Warmer"
    return intent.kind.replace("_", " ").capitalize()
