"""Intents in household terms: each kind with its defaults (tier, strength, how it ends),
built into the general shape.

Temporary intents always end: "warmer, please" at the next change of the pattern, at most a
day later; a bath when met, at the latest an hour after its time; "hands off" after at most
48 hours; a fireplace after 6 hours; a boost when its charge is done, at the latest after 4
hours.
"""

import secrets
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime, time, timedelta
from typing import Literal

from pydantic import JsonValue

from .model import Context, Expectation, Intent, Strength, Target, Validity

HANDS_OFF_MOST = timedelta(hours=48)
BATH_GRACE = timedelta(hours=1)
FIREPLACE = timedelta(hours=6)
BOOST_MOST = timedelta(hours=4)
SHIFT_STEPS = 2
"""Without a room sensor: how far price may shift the heat, in the pump's offset steps,
until the household sets its own bound."""

Addition = Literal["pump", "when_needed", "not_when_expensive", "limit"]
"""The pump's own logic; only when the compressor can't keep up; not in expensive hours;
within a power limit."""


def new_id() -> str:
    return f"in-{secrets.token_hex(6)}"


def _intent(
    kind: str,
    scope: str,
    principal: str,
    created: datetime,
    *,
    tier: str,
    expectations: Iterable[Expectation] = (),
    validity: Validity | None = None,
    strength: Strength | None = None,
    parameters: Mapping[str, JsonValue] | None = None,
    confirmed: bool = True,
) -> Intent:
    return Intent(
        id=new_id(),
        principal=principal,
        created=created,
        scope=scope,
        kind=kind,
        tier=tier,
        strength=strength,
        validity=validity or Validity(),
        expectations=tuple(expectations),
        parameters=dict(parameters or {}),
        confirmed=confirmed,
    )


Span = tuple[Sequence[int], time | None, time | None]
"""Days of the week (none: every day), and a start and end of day (none: all day)."""


def comfort_band(
    scope: str,
    pattern: Iterable[tuple[str, Span]],
    *,
    principal: str,
    created: datetime,
    season: tuple[str, str] | None = None,
    confirmed: bool = True,
) -> Intent:
    """A band for a climate system or a room, by level, on a weekly pattern."""
    expectations = [
        Expectation(
            targets=(Target(name="room_temp", condition="within", level=level, unit="degC"),),
            contexts=(Context(days=tuple(days), start=start, end=end, season=season),),
        )
        for level, (days, start, end) in pattern
    ]
    tier = "standing" if confirmed else "default"
    return _intent(
        "comfort_band",
        scope,
        principal,
        created,
        tier=tier,
        expectations=expectations,
        strength="should",
        confirmed=confirmed,
    )


def no_sensor_band(
    scope: str,
    *,
    steps: int = SHIFT_STEPS,
    principal: str,
    created: datetime,
    confirmed: bool = True,
) -> Intent:
    """Without a room sensor: normal heat, shifted by price at most `steps` either way."""
    return _intent(
        "comfort_band",
        scope,
        principal,
        created,
        tier="standing" if confirmed else "default",
        expectations=[
            Expectation(
                targets=(Target(name="offset_shift", condition="within", low=-steps, high=steps),)
            )
        ],
        strength="should",
        parameters={"no_sensor": True},
        confirmed=confirmed,
    )


def hot_water_by(
    scope: str,
    deadlines: Iterable[tuple[str | float, Sequence[int], time]],
    *,
    principal: str,
    created: datetime,
    strength: Strength = "should",
    confirmed: bool = True,
) -> Intent:
    """The tank's top at a level (or °C) by a time, on days of the week."""
    expectations = [
        Expectation(
            targets=(_tank(level),),
            contexts=(Context(days=tuple(days), by=by),),
        )
        for level, days, by in deadlines
    ]
    return _intent(
        "hot_water_by",
        scope,
        principal,
        created,
        tier="standing" if confirmed else "default",
        expectations=expectations,
        strength=strength,
        confirmed=confirmed,
    )


def hot_water_floor(
    scope: str, temp: float, *, principal: str, created: datetime, confirmed: bool = True
) -> Intent:
    """The lowest the tank's top may get, ever: a protection."""
    return _intent(
        "hot_water_floor",
        scope,
        principal,
        created,
        tier="protection",
        expectations=[
            Expectation(
                targets=(
                    Target(name="tank_top_temp", condition="at_least", value=temp, unit="degC"),
                )
            )
        ],
        confirmed=confirmed,
    )


def cost_stance(
    *, ranking: Sequence[str], slider: float, principal: str, created: datetime
) -> Intent:
    """What gives way first, and how much of the band price shifting may use (0 to 1)."""
    return _intent(
        "cost_stance",
        "house",
        principal,
        created,
        tier="standing",
        parameters={"ranking": list(ranking), "slider": slider},
    )


def addition_policy(
    policy: Addition,
    *,
    kw: float | None = None,
    principal: str,
    created: datetime,
    confirmed: bool = True,
) -> Intent:
    targets = [Target(name="addition", condition="equals", value=policy)]
    if kw is not None:
        targets.append(Target(name="addition", condition="at_most", value=kw, unit="kW"))
    return _intent(
        "addition_policy",
        "house",
        principal,
        created,
        tier="standing" if confirmed else "default",
        expectations=[Expectation(targets=(targets[0],))]
        + ([Expectation(targets=(targets[1],))] if kw is not None else []),
        strength="should",
        parameters={"kw": kw} if kw is not None else {},
        confirmed=confirmed,
    )


def pool(
    scope: str,
    level: str,
    *,
    pattern: Iterable[Span] = (((), None, None),),
    principal: str,
    created: datetime,
    confirmed: bool = True,
) -> Intent:
    return _intent(
        "pool",
        scope,
        principal,
        created,
        tier="standing" if confirmed else "default",
        expectations=[
            Expectation(
                targets=(Target(name="pool_temp", condition="within", level=level, unit="degC"),),
                contexts=tuple(Context(days=tuple(d), start=s, end=e) for d, s, e in pattern),
            )
        ],
        strength="should",
        confirmed=confirmed,
    )


def power_peak(
    kw: float, *, pattern: Iterable[Span] = (), principal: str, created: datetime
) -> Intent:
    """The house's power at most `kw`, at the times given (none: always)."""
    return _intent(
        "power_peak",
        "house",
        principal,
        created,
        tier="standing",
        expectations=[
            Expectation(
                targets=(Target(name="house_power", condition="at_most", value=kw, unit="kW"),),
                contexts=tuple(Context(days=tuple(d), start=s, end=e) for d, s, e in pattern),
            )
        ],
        strength="should",
    )


def quiet_hours(pattern: Iterable[Span], *, principal: str, created: datetime) -> Intent:
    return _intent(
        "quiet_hours",
        "house",
        principal,
        created,
        tier="standing",
        expectations=[
            Expectation(
                targets=(Target(name="quiet", condition="equals", value=True),),
                contexts=tuple(Context(days=tuple(d), start=s, end=e) for d, s, e in pattern),
            )
        ],
        strength="should",
    )


# --- temporary ----------------------------------------------------------------------------


def warmer(
    scope: str,
    offset: float,
    *,
    principal: str,
    created: datetime,
    until: datetime | None = None,
) -> Intent:
    """+n or -n °C on whatever band is in force, until the next change of the pattern (at
    most a day)
    unless a time is given."""
    validity = Validity(ends="at", at=until) if until else Validity(ends="next_change")
    return _intent(
        "warmer",
        scope,
        principal,
        created,
        tier="temporary",
        validity=validity,
        strength="should",
        parameters={"offset": offset},
    )


def bath(
    scope: str,
    at_least: float,
    by: datetime,
    *,
    principal: str,
    created: datetime,
    strength: Strength = "must",
) -> Intent:
    """The tank's top at least `at_least` °C by `by`; done when met, or an hour after."""
    return _intent(
        "bath",
        scope,
        principal,
        created,
        tier="temporary",
        expectations=[Expectation(targets=(_tank(at_least),), contexts=(Context(at=by),))],
        validity=Validity(ends="when_met", at=by + BATH_GRACE),
        strength=strength,
    )


def guests(
    until: datetime,
    levels: Mapping[str, str],
    *,
    hot_water: str | None = None,
    principal: str,
    created: datetime,
) -> Intent:
    """The bands replaced by the levels picked, per climate system, and more hot water
    (a tank level) until a date."""
    parameters: dict[str, JsonValue] = {"levels": dict(levels)}
    if hot_water is not None:
        parameters["hot_water"] = hot_water
    return _intent(
        "guests",
        "house",
        principal,
        created,
        tier="temporary",
        validity=Validity(ends="at", at=until),
        strength="should",
        parameters=parameters,
    )


def away(
    until: datetime, levels: Mapping[str, str], *, principal: str, created: datetime
) -> Intent:
    """The bands replaced by the levels picked, hot water down to its floor, and the house
    ready again by the return."""
    return _intent(
        "away",
        "house",
        principal,
        created,
        tier="temporary",
        validity=Validity(ends="at", at=until),
        strength="should",
        parameters={"levels": dict(levels)},
    )


def hands_off(until: datetime, *, principal: str, created: datetime) -> Intent:
    """Thermaestro changes nothing on the pump until `until`, at most 48 hours."""
    return _intent(
        "hands_off",
        "house",
        principal,
        created,
        tier="hands_off",
        validity=Validity(ends="at", at=min(until, created + HANDS_OFF_MOST)),
    )


def fireplace(scope: str, *, principal: str, created: datetime) -> Intent:
    """Another heat source is warming the house: comfort eases off for a while."""
    return _intent(
        "fireplace",
        scope,
        principal,
        created,
        tier="temporary",
        validity=Validity(ends="at", at=created + FIREPLACE),
    )


def boost_now(scope: str, *, principal: str, created: datetime) -> Intent:
    """One charge, now."""
    return _intent(
        "boost_now",
        scope,
        principal,
        created,
        tier="temporary",
        validity=Validity(ends="when_met", at=created + BOOST_MOST),
    )


def _tank(level: str | float) -> Target:
    if isinstance(level, str):
        return Target(name="tank_top_temp", condition="at_least", level=level, unit="degC")
    return Target(name="tank_top_temp", condition="at_least", value=level, unit="degC")
