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

Each part of the answer is a message marked for translation with its values (`Said`), so
a page says it in its reader's language; `Verdict.messages` is the same in English.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from pydantic import JsonValue

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


def mark(text: str) -> str:
    """Marks a message for the web UI's catalog: shown translated, kept in English here."""
    return text


EMITTER_WORDS = {
    "radiators": mark("the radiators"),
    "fan_coils": mark("the fan coils"),
    "floor_light": mark("the floor heating"),
    "slab": mark("the floor heating"),
    "radiators_and_floor_light": mark("the heating"),
    "radiators_and_slab": mark("the heating"),
    "unknown": mark("the heating"),
}
NAMES = {
    "warmer": mark("Warmer, please"),
    "bath": mark("Bath"),
    "guests": mark("Guests"),
    "away": mark("Away"),
    "hands_off": mark("Hands off"),
    "fireplace": mark("Fireplace"),
    "boost_now": mark("Boost now"),
    "comfort_band": mark("Comfort band"),
    "hot_water_by": mark("Hot water by"),
    "hot_water_floor": mark("Hot water floor"),
    "cost_stance": mark("Cost stance"),
    "addition_policy": mark("Addition policy"),
    "pool": mark("Pool"),
    "power_peak": mark("Power peak"),
    "quiet_hours": mark("Quiet hours"),
}
COOLER = mark("Cooler, please")
"""How an intent is named in an answer."""

MQTT_STANDING = mark("a request over MQTT is for a while: standing intents are set in the UI")
NO_POWER = mark("a power limit needs a whole-house power reading; there is none yet")
ENDED = mark("it would already have ended")
NO_LEVEL = mark("there is no level '%(level)s'")
CONTRADICTS = mark(
    "it contradicts what is set for %(scope)s: %(target)s at least %(low)s and at most %(high)s,"
    " %(when)s"
)
HANDS_OFF_MOST_SAID = mark("hands off lasts at most 48 hours")
SET_ASIDE = mark("%(name_label)s (%(principal)s) is set aside meanwhile")
SET_ASIDE_OFFSET = mark("%(name_label)s (%(offset)s °C) (%(principal)s) is set aside meanwhile")
UNTIL_DONE = mark("%(name_label)s: until done, at the latest %(when)s")
UNTIL_DONE_OFFSET = mark("%(name_label)s (%(offset)s °C): until done, at the latest %(when)s")
UNTIL_CHANGE = mark("%(name_label)s: until the next change, %(when)s")
UNTIL_CHANGE_OFFSET = mark("%(name_label)s (%(offset)s °C): until the next change, %(when)s")
UNTIL = mark("%(name_label)s: until %(when)s")
UNTIL_OFFSET = mark("%(name_label)s (%(offset)s °C): until %(when)s")
NOT_CHANGEABLE = mark("%(system)s's heating can't be changed: shown, not acted on")
NO_ROOM_SENSOR = mark(
    "%(system)s has no room sensor: normal heat, shifted by price within a bound instead of a band"
)
NO_BLOCK = mark("without a hot-water block, charges can only be added when cheap, not held off")
NO_PRICES = mark("no prices yet: comfort only, no shifting by price")
NO_ADDITION = mark("the addition can't be changed here: the pump's own logic stays")
NO_POOL = mark("the pool can't be changed here: the pump's own pool control stays")
WARMER_IN = mark("%(emitter_label)s will be %(offset)s °C warmer in about %(hours)s hours")
COOLER_IN = mark("%(emitter_label)s will be %(offset)s °C cooler in about %(hours)s hours")

Said = tuple[str, dict[str, JsonValue]]
"""A message marked for translation, and its values."""


def say(message: str, **args: JsonValue) -> Said:
    return message, args


def english(said: Said) -> str:
    message, args = said
    if not args:
        return message
    shown = {k: v for k, v in args.items()}
    return message % shown


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
    response_h: Mapping[str, float] = field(default_factory=dict)
    """How long each climate system's heat takes to show, where learned and proved."""


@dataclass(frozen=True)
class Verdict:
    intent: Intent
    accepted: bool
    said: tuple[Said, ...] = ()
    """What the answer says, each part a message and its values."""

    @property
    def messages(self) -> tuple[str, ...]:
        """What the answer says, in English."""
        return tuple(english(s) for s in self.said)


def check(
    intent: Intent,
    others: list[Intent],
    levels: Mapping[str, Level],
    caps: Capabilities,
    calendar: Calendar,
    now: datetime,
) -> Verdict:
    messages: list[Said] = []

    def refuse(why: Said) -> Verdict:
        update = {
            "state": "rejected",
            "why": english(why),
            "why_message": why[0],
            "why_args": why[1],
        }
        return Verdict(intent.model_copy(update=update), False, (why, *messages))

    if intent.principal == "mqtt" and intent.kind in STANDING:
        return refuse(say(MQTT_STANDING))
    if intent.kind == "power_peak" and not caps.power:
        return refuse(say(NO_POWER))
    intent, clipped = _definite_end(intent)
    if clipped:
        messages.append(clipped)
    end = Resolver([intent], levels, calendar).end(intent)
    if end is not None and end <= now:
        return refuse(say(ENDED))
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
            name = named(other)
            asked = {**name, "principal": other.principal}
            messages.append(say(SET_ASIDE_OFFSET if "offset" in name else SET_ASIDE, **asked))
    if intent.kind == "warmer":
        messages.extend(_reach(intent, caps))
    until = _until(intent, resolver, calendar)
    if until is not None:
        messages.insert(0, until)
    state = "scheduled" if intent.validity.starts and intent.validity.starts > now else "active"
    accepted = intent.model_copy(update={"state": state, "why": None})
    return Verdict(accepted, True, tuple(messages))


def _definite_end(intent: Intent) -> tuple[Intent, Said | None]:
    v = intent.validity
    if intent.kind == "hands_off":
        start = v.starts or intent.created
        latest = start + HANDS_OFF_MOST
        if v.at is None or v.at > latest:
            ended = Validity(starts=v.starts, ends="at", at=latest)
            return intent.model_copy(update={"validity": ended}), say(HANDS_OFF_MOST_SAID)
    return intent, None


def _missing_levels(intent: Intent, levels: Mapping[str, Level]) -> Said | None:
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
            return say(NO_LEVEL, level=level_id)
    return None


def _contradiction(
    intent: Intent,
    others: list[Intent],
    levels: Mapping[str, Level],
    calendar: Calendar,
    now: datetime,
) -> Said | None:
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
                return say(
                    CONTRADICTS,
                    scope=scope,
                    target=target.replace("_", " "),
                    low=_number(bound.low),
                    high=_number(bound.high),
                    when=f"{at:%A %H:%M}",
                )
        t += STEP
    return None


def _gaps(intent: Intent, caps: Capabilities) -> list[Said]:
    """What the house lacks for the intent, and what happens instead."""
    out = []
    systems = caps.systems if intent.scope == "house" else {intent.scope} & caps.systems
    if intent.kind in ("comfort_band", "warmer"):
        for system in sorted(systems):
            if system not in caps.offset:
                out.append(say(NOT_CHANGEABLE, system=system))
            elif not any(s == system for s in caps.rooms.values()) and not intent.parameters.get(
                "no_sensor"
            ):
                out.append(say(NO_ROOM_SENSOR, system=system))
    if intent.kind in ("hot_water_by", "bath") and intent.scope not in caps.block:
        out.append(say(NO_BLOCK))
    if intent.kind == "cost_stance" and not caps.prices:
        out.append(say(NO_PRICES))
    if intent.kind == "addition_policy" and not caps.addition:
        out.append(say(NO_ADDITION))
    if intent.kind == "pool" and intent.scope not in caps.pools:
        out.append(say(NO_POOL))
    return out


def _reach(intent: Intent, caps: Capabilities) -> list[Said]:
    """On a slow system, how long a warmer or cooler takes to show."""
    offset = intent.parameters.get("offset")
    if not isinstance(offset, int | float):
        return []
    systems = sorted(caps.systems if intent.scope == "house" else {intent.scope})
    out = []
    for system in systems:
        emitter = caps.emitters.get(system, "unknown")
        hours = caps.response_h.get(system) or RESPONSE_H.get(emitter, RESPONSE_H["unknown"])
        hours = round(hours)
        if hours >= 4:
            label = EMITTER_WORDS.get(emitter, EMITTER_WORDS["unknown"])
            said = WARMER_IN if offset > 0 else COOLER_IN
            out.append(say(said, emitter_label=label, offset=_number(abs(offset)), hours=hours))
    return out


def _until(intent: Intent, resolver: Resolver, calendar: Calendar) -> Said | None:
    end = resolver.end(intent)
    if end is None:
        return None
    when = f"{calendar.local(end):%Y-%m-%d %H:%M}"
    name = named(intent)
    with_offset = "offset" in name
    if intent.validity.ends == "when_met":
        said = UNTIL_DONE_OFFSET if with_offset else UNTIL_DONE
    elif intent.validity.ends == "next_change":
        said = UNTIL_CHANGE_OFFSET if with_offset else UNTIL_CHANGE
    else:
        said = UNTIL_OFFSET if with_offset else UNTIL
    return say(said, **name, when=when)


def named(intent: Intent) -> dict[str, JsonValue]:
    """An intent's name in an answer: its label, and for warmer or cooler its offset."""
    if intent.kind == "warmer":
        offset = intent.parameters.get("offset")
        if isinstance(offset, int | float):
            label = COOLER if offset < 0 else NAMES["warmer"]
            return {"name_label": label, "offset": f"{offset:+g}"}
    return {"name_label": NAMES.get(intent.kind, intent.kind.replace("_", " ").capitalize())}


def _number(value: float | None) -> JsonValue:
    """A whole number as one, as it would be said."""
    if value is None:
        return None
    return int(value) if float(value).is_integer() else value
