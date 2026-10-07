"""What a plugin describes, and the values the core receives.

A plugin speaks for its devices in these terms only: a tree of nodes, points to observe,
levers to act with, and series of intervals. The core never sees registers, frames or
URLs. Every fact about a device carries what is known about it, because for most devices
little is known at the start.
"""

from datetime import date
from typing import Annotated, Any, Literal, Self, get_args, get_origin

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


_SEGMENT = r"[A-Za-z0-9_.-]+(\{[A-Za-z0-9_.=,-]+\})?(#[0-9]+)?"
Path = Annotated[str, StringConstraints(pattern=rf"^{_SEGMENT}(/{_SEGMENT})*$")]
"""A node, point or lever: `hp1`, `hp1/dhw/temp.top`, `room.living/temperature#2`.
The plugin chooses it and keeps it across restarts and updates."""

Vendor = Annotated[str, StringConstraints(pattern=r"^x\.[a-z0-9_]+\.[A-Za-z0-9_.-]+$")]
"""A name outside the standard vocabulary, under the plugin's own namespace: `x.nibe.47134`."""

PluginName = Annotated[str, StringConstraints(pattern=r"^[a-z0-9_]+$")]


# --- Knowledge -------------------------------------------------------------------------

Known = Literal["documented", "verified", "observed", "user", "reported", "refuted", "unknown"]
"""How a fact is known: a vendor document, a test on this model and firmware, seen in
normal running, confirmed by the user, a third-party source, tested and found false, or
not at all."""

TRUSTED: frozenset[Known] = frozenset({"documented", "verified", "user"})
"""Knowledge that may make the core less careful. Any knowledge may make it more careful."""


class Knowledge[T](Model):
    """A fact with what is known about it. Unknown carries no value."""

    value: T | None = None
    known: Known = "unknown"
    basis: str | tuple[str, ...] = ()
    """Where it came from: document and page, test and date, model and firmware. Sources
    that disagree are all listed."""

    @model_validator(mode="after")
    def _value_matches_known(self) -> Self:
        if (self.known == "unknown") != (self.value is None):
            raise ValueError("a value needs a knowledge state other than unknown, and only then")
        return self

    @property
    def trusted(self) -> bool:
        return self.known in TRUSTED

    @classmethod
    def model_parametrized_name(cls, params: tuple[Any, ...]) -> str:
        # The JSON Schema's definition names, read by plugin authors in other languages.
        return f"Knowledge{_readable(params[0])}"


def _readable(t: Any) -> str:
    origin, args = get_origin(t), get_args(t)
    if origin is tuple:
        return f"{_readable(args[0])}List"
    if origin is dict:
        return "EnumMap"
    if origin is Literal:
        return "".join(str(a).capitalize() for a in args)
    name = str(getattr(t, "__name__", "Value"))
    return name[:1].upper() + name[1:]


class Range(Model):
    min: float | None = None
    max: float | None = None
    step: float | None = None


# --- The device tree --------------------------------------------------------------------

NodeKind = (
    Literal[
        "site",
        "unit",
        "climate_system",
        "dhw_tank",
        "pool",
        "compressor",
        "addition",
        "brine_circuit",
        "ventilation",
        "room",
        "meter",
        "output",
    ]
    | Vendor
)

SharedConstraint = Literal["one_mode_at_a_time", "one_demand_at_a_time"] | Vendor
"""A limit a node shares with its siblings: multi-split indoor units that serve one mode
at a time, or demands that compete for one compressor."""


class Presence(Model):
    """How a node is known to exist. Never from "the register answers": some pumps answer
    for hardware that isn't fitted."""

    how: Literal["detected", "configured", "assumed"]
    rule: str | None = None
    """The detection rule, for a detected node."""

    @model_validator(mode="after")
    def _detected_has_rule(self) -> Self:
        if self.how == "detected" and not self.rule:
            raise ValueError("a detected node names the rule that detected it")
        return self


class Identity(Model):
    """A unit's identity. The core keys learned knowledge on plugin, model and firmware."""

    vendor: str
    model: str
    firmware: str | None = None
    serial: str | None = None
    map: str | None = None
    """The register map or equivalent the plugin resolved for this unit."""


class Ack(Model):
    means: Literal["accepted", "applied", "none"]
    """What the device's acknowledgment of a write says. Accepted isn't applied."""
    detail: str | None = None


class Window(Model):
    """A call limit: at most `calls` in any `per_s` seconds."""

    calls: Annotated[int, Field(gt=0)]
    per_s: Annotated[float, Field(gt=0)]
    sliding: bool = True


Seen = Literal["yes", "no", "partial"]


class Promises(Model):
    """What a route can know and guarantee, so the core can plan reads and trust results."""

    fate: Literal["none", "best_effort", "exact"]
    ack: Ack
    exclusive: Literal["none", "refuse_new", "evict_old"] = "none"
    """Whether the route excludes other clients: a second one is turned away, or the
    newest wins. After an eviction a plugin doesn't reconnect by itself."""
    sees_other_writers: Knowledge[Seen] = Knowledge[Seen]()
    budget: Knowledge[tuple[Window, ...]] = Knowledge[tuple[Window, ...]]()
    """Call limits shared by reads and writes; every window applies."""
    deadman: Literal["supported", "unsupported"] = "unsupported"
    """Whether the route can restore settings by itself if the core goes silent."""


class Node(Model):
    path: Path
    kind: NodeKind
    presence: Presence
    label: str | None = None
    identity: Identity | None = None
    transport: Promises | None = None
    constraints: tuple[SharedConstraint, ...] = ()


# --- Points -------------------------------------------------------------------------------


class Delivery(Model):
    """How a point's values arrive: pushed unasked, polled at a cost, or on change."""

    how: Literal["pushed", "polled", "on_change"]
    interval_s: Annotated[float, Field(gt=0)] | None = None
    cost_s: Annotated[float, Field(ge=0)] | None = None

    @model_validator(mode="after")
    def _has_its_numbers(self) -> Self:
        if self.how == "pushed" and self.interval_s is None:
            raise ValueError("a pushed point gives its interval")
        if self.how == "polled" and self.cost_s is None:
            raise ValueError("a polled point gives the cost of a read")
        return self


EnumMap = dict[str, int | str]
"""Standard value to device value."""


class Point(Model):
    path: Path
    label: str | None = None
    """A name for people where the path's standard name doesn't give one: a device
    register's own title, for a point under `x.<plugin>`."""
    description: str | None = None
    """What the device's own documentation says the point is, for people."""
    unit: str | None = None
    wraps_at: Annotated[float, Field(gt=0)] | None = None
    """For a counter: the value at which it starts over from 0, from the size of the
    device's register. A drop of more than half of it is a wrap, not a counter running
    backwards."""
    resolution: Knowledge[float] = Knowledge[float]()
    range: Knowledge[Range] = Knowledge[Range]()
    enum: Knowledge[EnumMap] = Knowledge[EnumMap]()
    delivery: Delivery
    freshness_s: Knowledge[float] = Knowledge[float]()
    """How old a value may get before it is stale."""
    validity: tuple[str, ...] = ()
    """The rules the plugin applies to a value's quality, listed for explanations."""
    placement: str | None = None
    """For a sensor: where it sits, and whether that is representative."""


# --- Levers -------------------------------------------------------------------------------

LeverKind = Literal["setting", "hold", "trigger", "feed"]
"""A value that stays until changed; a state engaged and then released, restoring what was
there; a one-shot request the device completes by itself; a value supplied continuously,
which lapses if not renewed."""


class Param(Model):
    type: Literal["number", "enum", "bool"]
    unit: str | None = None
    range: Knowledge[Range] = Knowledge[Range]()
    enum: Knowledge[EnumMap] = Knowledge[EnumMap]()
    per_mode: dict[str, Knowledge[Range]] = {}
    """Ranges that differ by the device's mode."""
    form: Literal["absolute", "toggle"] = "absolute"
    """A toggle flips a state, so it is only sent when that state is known to be good."""


class Persistence(Model):
    kind: Literal["stored", "volatile", "leased", "momentary"]
    period_s: Annotated[float, Field(gt=0)] | None = None
    """For a leased lever: renew within this, or it lapses."""

    @model_validator(mode="after")
    def _leased_has_period(self) -> Self:
        if (self.kind == "leased") != (self.period_s is not None):
            raise ValueError("a leased lever gives its period, and only it")
        return self


class OnLapse(Model):
    """What the device does when a lease lapses."""

    kind: Literal["reverts", "holds", "alarms", "unknown"]
    to: tuple[str, ...] = ()
    """What it reverts to, in order: a fallback chain."""
    hazard: bool = False
    """The chain ends in a fixed value, such as 0 °C if a fallback sensor is missing."""


class Arming(Model):
    """A setting that must be in place before the lever works, restored at release."""

    datapoint: str
    value: int | float | str


class Wear(Model):
    kind: Literal["none", "flash", "cloud_calls"]
    per_call: Annotated[int, Field(ge=1)] = 1


class Implementation(Model):
    kind: Literal["native", "emulated"]
    how: str | None = None
    side_effects: tuple[str, ...] = ()


class Verify(Model):
    """How a result can be checked: read back, watch an effect, or not at all."""

    kind: Literal["readback", "effect", "none"]
    point: Path | None = None
    expectation: str | None = None

    @model_validator(mode="after")
    def _names_what_it_watches(self) -> Self:
        if self.kind != "none" and self.point is None:
            raise ValueError(f"{self.kind} verification names the point it watches")
        if self.kind == "effect" and not self.expectation:
            raise ValueError("effect verification says what it expects")
        return self


class CompetingFeature(Model):
    """A device or route feature that moves the same thing as the lever."""

    name: str
    can_disable: Knowledge[bool] = Knowledge[bool]()
    how: str | None = None


class Lever(Model):
    path: Path
    kind: LeverKind
    params: dict[str, Param] = {}
    works: Knowledge[bool] = Knowledge[bool]()
    """Whether the lever does what its name says."""
    preconditions: Knowledge[tuple[str, ...]] = Knowledge[tuple[str, ...]]()
    persistence: Knowledge[Persistence] = Knowledge[Persistence]()
    on_lapse: Knowledge[OnLapse] = Knowledge[OnLapse]()
    arming: tuple[Arming, ...] = ()
    min_interval_s: Knowledge[float] = Knowledge[float]()
    overridden_by: Knowledge[tuple[str, ...]] = Knowledge[tuple[str, ...]]()
    """Things outside software that win over the lever, so follow-up must expect them."""
    wear: Knowledge[Wear] = Knowledge[Wear]()
    implementation: Implementation = Implementation(kind="native")
    verify: Verify
    effect_delay_s: Knowledge[float] = Knowledge[float]()
    irreversible: Knowledge[bool] = Knowledge[bool]()
    competing_features: tuple[CompetingFeature, ...] = ()
    touches: tuple[str, ...] = ()
    """The datapoints it writes, in the plugin's own terms. Two levers whose lists overlap
    can't be taken over together."""
    baseline: bool | None = None
    """Whether the core records what it found before the first use; by default it does
    for settings and holds."""
    hazard: Literal["control_sensor"] | None = None
    """A feed into a sensor the device controls with."""
    unavailable: str | None = None
    """Why the lever can't be used on this route, if it can't."""

    @model_validator(mode="after")
    def _lists_what_it_touches(self) -> Self:
        if not self.touches and self.unavailable is None:
            raise ValueError("a lever lists the datapoints it touches")
        return self

    @property
    def needs_baseline(self) -> bool:
        return self.baseline if self.baseline is not None else self.kind in ("setting", "hold")


# --- Values -------------------------------------------------------------------------------

Quality = Literal[
    "good",
    "stale",
    "not_connected",
    "no_flow",
    "transitional",
    "assumed",
    "out_of_range",
    "unknown",
]
"""Only good may drive a decision; anything else is shown and logged."""

Source = Literal["measured", "calculated", "estimated", "assumed"]

Value = bool | int | float | str


class Envelope(Model):
    """One value, as every read, subscription and verification delivers it."""

    point: Path
    value: Value | None
    unit: str | None = None
    raw: int | float | str | None = None
    """The device's value before scaling, for tracing. Never planned on."""
    t_observed: AwareDatetime | None
    """When the device sampled it, as near the source as the route allows; None if
    unknown."""
    t_received: AwareDatetime
    quality: Quality
    source: Source
    resolution: float | None = None
    why: str | None = None
    """A short machine reason for a quality that isn't good."""

    @model_validator(mode="after")
    def _sentinels_never_arrive_as_numbers(self) -> Self:
        if self.quality == "good" and self.value is None:
            raise ValueError("a good value has a value")
        if self.quality == "not_connected" and self.value is not None:
            raise ValueError("a sensor that isn't connected has no value")
        return self


# --- Series -------------------------------------------------------------------------------

Duration = Annotated[
    str, StringConstraints(pattern=r"^P(\d+D(T\d+H(\d+M)?|T\d+M)?|T\d+H(\d+M)?|T\d+M)$")
]
"""An ISO 8601 duration in days, hours and minutes: `PT15M`, `P1D`."""


class Publication(Model):
    daily_after: Annotated[str, StringConstraints(pattern=r"^([01]\d|2[0-3]):[0-5]\d$")]
    tz: str


class SeriesInfo(Model):
    """A series a plugin offers: a layer of the price stack, a grid rule or a forecast."""

    id: str
    kind: Literal["price", "rule", "forecast"]
    role: Annotated[str, StringConstraints(pattern=r"^[a-z_]+(\.[a-z_]+)*$")]
    covers: Knowledge[tuple[str, ...]] = Knowledge[tuple[str, ...]]()
    """The roles already inside this series' value."""
    unit: str
    vat: Literal["incl", "excl", "n/a"] = "n/a"
    resolution: Duration
    area: str | None = None
    publication: Publication | None = None
    horizon: Duration | None = None


class Interval(Model):
    series: str
    start: AwareDatetime
    end: AwareDatetime
    value: float
    unit: str
    vat: Literal["incl", "excl", "n/a"] = "n/a"
    status: Literal["final", "preliminary", "estimate", "forecast"]
    revision: Annotated[int, Field(ge=1)] = 1
    t_published: AwareDatetime | None = None
    source: Literal["derived", "calculated"] | None = None
    """None for a value as its source published it."""
    why: str | None = None

    @model_validator(mode="after")
    def _ends_after_start(self) -> Self:
        if self.end <= self.start:
            raise ValueError("an interval ends after it starts")
        return self


class RuleRecord(Model):
    """A grid rule as data, each parameter with what is known about it."""

    type: str
    owner: str | None = None
    status: Literal["in_force", "announced", "paused", "withdrawn"]
    valid_from: date | None = None
    valid_to: date | None = None
    parameters: dict[str, Knowledge[JsonValue]] = {}
