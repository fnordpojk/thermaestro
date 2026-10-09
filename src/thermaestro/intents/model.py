"""What the household wants, as intents: who asked, for what part of the house, what
they want there and when, how strongly, and until when.

An intent has expectations; each has targets (the room within 20.5 to 22 °C, the tank's top
at least 50 °C) and the contexts in which they apply (weekdays 06:00 to 22:00; by 19:30
today). Targets and contexts are never mixed: "22 °C when someone is home" doesn't mean
"make someone home".

Scopes name parts of the house: `house`; a climate system, tank or pool by its node
(`pump:hp1/cs1`, `pump:hp1/dhw`, `pump:hp1/pool1`); a room as `room:<id>`.
"""

from datetime import UTC, datetime, time
from typing import Annotated, Literal, Self

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    StringConstraints,
    model_validator,
)


class Model(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


Scope = Annotated[str, StringConstraints(pattern=r"^(house|room:[a-z0-9_-]+|[a-z0-9_]+:[^\s]+)$")]

Tier = Literal["protection", "wear", "hands_off", "temporary", "standing", "default"]
"""P0 to P5: what an intent is decides its place, not a number anyone picks."""
TIERS: tuple[Tier, ...] = ("protection", "wear", "hands_off", "temporary", "standing", "default")

Strength = Literal["must", "should"]

Standing = Literal[
    "comfort_band",
    "hot_water_by",
    "hot_water_floor",
    "cost_stance",
    "addition_policy",
    "pool",
    "power_peak",
    "quiet_hours",
]
Temporary = Literal["warmer", "bath", "guests", "away", "hands_off", "fireplace", "boost_now"]
Kind = Standing | Temporary
STANDING: frozenset[str] = frozenset(Standing.__args__)  # type: ignore[attr-defined]
TEMPORARY: frozenset[str] = frozenset(Temporary.__args__)  # type: ignore[attr-defined]

State = Literal[
    "received",
    "rejected",
    "scheduled",
    "active",
    "at_risk",
    "giving_way",
    "met",
    "missed",
    "finished",
]
"""Paused (shadowed by another intent, or hands off) isn't kept: it is worked out from
the others when they are resolved."""
OPEN: frozenset[str] = frozenset({"received", "scheduled", "active", "at_risk", "giving_way"})

TargetName = Literal[
    "room_temp",
    "offset_shift",
    "tank_top_temp",
    "pool_temp",
    "house_power",
    "addition",
    "quiet",
]
Condition = Literal["within", "at_least", "at_most", "equals"]


class Target(Model):
    name: TargetName
    condition: Condition
    low: float | None = None
    high: float | None = None
    value: float | str | bool | None = None
    level: str | None = None
    """A level's id, standing for its numbers: changing the level changes every target
    that names it."""
    unit: str | None = None

    @model_validator(mode="after")
    def _says_what(self) -> Self:
        if self.level is not None:
            return self
        if self.condition == "within" and (self.low is None or self.high is None):
            raise ValueError("a range has a low and a high end, or names a level")
        if self.condition != "within" and self.value is None:
            raise ValueError(f"{self.condition} needs a value, or a level")
        if self.low is not None and self.high is not None and self.low > self.high:
            raise ValueError(f"the low end {self.low:g} is above the high end {self.high:g}")
        return self


Day = Annotated[int, Field(ge=0, le=6)]
"""0 Monday to 6 Sunday."""
MonthDay = Annotated[str, StringConstraints(pattern=r"^(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$")]


class Context(Model):
    """When targets apply. Everything given must hold; an empty context always holds."""

    days: tuple[Day, ...] = ()
    """The days of the week, in the house's time; none: every day."""
    start: time | None = None
    end: time | None = None
    """Local times of day; an end before the start runs past midnight."""
    at: AwareDatetime | None = None
    """One moment: a deadline."""
    by: time | None = None
    """A local time on each of `days`: a deadline that comes back."""
    season: tuple[MonthDay, MonthDay] | None = None
    """From and to, inclusive; a range past New Year wraps."""
    outdoor_below: float | None = None
    outdoor_above: float | None = None

    @model_validator(mode="after")
    def _one_kind_of_when(self) -> Self:
        if self.at is not None and (self.by is not None or self.start or self.end or self.days):
            raise ValueError("a moment can't come back on days or times as well")
        if self.by is not None and (self.start is not None or self.end is not None):
            raise ValueError("a deadline is a time, not a span")
        return self

    @property
    def deadline(self) -> bool:
        return self.at is not None or self.by is not None


class Expectation(Model):
    targets: Annotated[tuple[Target, ...], Field(min_length=1)]
    contexts: tuple[Context, ...] = ()
    """Any of them; none: always."""


Ends = Literal["at", "next_change", "when_met", "never"]


class Validity(Model):
    starts: AwareDatetime | None = None
    ends: Ends = "never"
    at: AwareDatetime | None = None
    """For `at`, the end; for `when_met`, the latest it lasts."""

    @model_validator(mode="after")
    def _end_has_its_time(self) -> Self:
        if self.ends in ("at", "when_met") and self.at is None:
            raise ValueError(f"an intent that ends {self.ends.replace('_', ' ')} gives a time")
        if self.starts is not None and self.at is not None and self.at <= self.starts:
            raise ValueError("it would end before it starts")
        return self


class Intent(Model):
    id: Annotated[str, StringConstraints(pattern=r"^[a-z0-9-]{1,64}$")]
    principal: str
    """Who asked: `user:<name>`, `mqtt`, `seed`."""
    created: AwareDatetime
    scope: Scope
    kind: Kind
    tier: Tier
    strength: Strength | None = None
    validity: Validity = Validity()
    expectations: tuple[Expectation, ...] = ()
    parameters: dict[str, JsonValue] = Field(default_factory=dict)
    """What the kind takes besides targets: the offset of "warmer", the ranking and the
    slider of the cost stance, the levels "away" picks."""
    state: State = "received"
    why: str | None = None
    """What the state means here: why it was rejected, what it is short by."""
    confirmed: bool = True
    """False for what was seeded or learned and not yet confirmed: it stays a default."""

    @model_validator(mode="after")
    def _kind_fits_its_tier(self) -> Self:
        temporary = self.kind in TEMPORARY
        if temporary and self.tier not in ("temporary", "hands_off"):
            raise ValueError(f"{self.kind} is a temporary intent")
        if self.kind == "hands_off" and self.tier != "hands_off":
            raise ValueError("hands off has its own tier")
        if not temporary and self.tier in ("temporary", "hands_off"):
            raise ValueError(f"{self.kind} is a standing intent")
        if temporary and self.validity.ends == "never":
            raise ValueError("a temporary intent always has an end")
        return self

    @property
    def open(self) -> bool:
        return self.state in OPEN


class Level(Model):
    """A named band for a climate system or a room, or a named top temperature for the
    tank or a pool's band: the household's own, used by name in patterns and intents."""

    id: Annotated[str, StringConstraints(pattern=r"^[a-z0-9-]{1,64}$")]
    name: Annotated[str, StringConstraints(min_length=1, max_length=64)]
    scope: Scope
    low: float | None = None
    high: float | None = None
    top: float | None = None
    """For the tank: the top temperature."""

    @model_validator(mode="after")
    def _band_or_top(self) -> Self:
        band = self.low is not None and self.high is not None
        if band == (self.top is not None):
            raise ValueError("a level is a band (low and high) or a tank's top temperature")
        if band and self.low > self.high:  # type: ignore[operator]
            raise ValueError(f"the low end {self.low:g} is above the high end {self.high:g}")
        return self


EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
