"""What the optimizer plans over, and what it plans: plain data, so a plan can be made, kept
and checked without a running core.

The horizon is 15-minute slots in whole hours. The house is planned by the hour, as its
model was learned; the compressor's time, the tanks and the pools by the slot.
"""

from dataclasses import dataclass, field

from ..learn.house import Fit
from ..learn.pump import Water

SLOT_H = 0.25


@dataclass(frozen=True)
class Slot:
    t: float
    """Its start, seconds since the epoch."""
    price: float | None = None
    """What a kWh costs, VAT included; None where it isn't known."""
    estimated: bool = False
    """The price not published yet: from recent weeks."""
    outdoor: float = 0.0
    sun: float = 0.0
    """W/m², global horizontal."""
    wind: float = 0.0
    source: float = 0.0
    """The heat source's temperature: the brine coming in, or the outdoor air."""
    power_limit: float | None = None
    """The most the house may draw, kW."""
    other_kw: float = 0.0
    """The household's own load, expected."""
    expensive: bool = False
    """An expensive slot, for the addition's "not when expensive"."""


@dataclass(frozen=True)
class Band:
    low: float | None = None
    high: float | None = None


@dataclass(frozen=True)
class Room:
    scope: str
    off: float
    """How far it sits from its climate system's rooms' mean, now: kept so."""
    bands: tuple[Band, ...]
    """Per hour of the horizon."""
    name: str = ""


@dataclass(frozen=True)
class System:
    """A climate system with its house model learned."""

    scope: str
    fit: Fit
    water: Water
    emitters_kw_k: float
    offset_low: float
    offset_high: float
    offset_now: float
    room: float
    """Its rooms' mean now."""
    water_before: tuple[float, ...]
    """The water in the hours before this one, oldest first, for the model's lag."""
    rooms: tuple[Room, ...]
    share: float
    """Its COP as a share of the ideal."""
    draw_kw: float
    """What the compressor draws heating it."""
    emitters_kwh_k: float = 0.0
    """What its emitters store per degree of their water."""
    drift: float = 0.0
    """What the rooms have done that the house model didn't foresee, °C an hour, from how
    the last plans came out: carried into this one."""


@dataclass(frozen=True)
class Deadline:
    slot: int
    """The slot it falls at the end of."""
    at_least: float
    rank: int
    by: str = ""


@dataclass(frozen=True)
class Tank:
    scope: str
    level: float
    """Its temperature now, in degrees of the tank (the mean of its two sensors)."""
    kwh_per_k: float
    loss: float
    """Its standing loss: the share of its lead over the room it loses an hour."""
    draws: tuple[float, ...]
    """Degrees of the tank expected drawn in each slot."""
    ceiling: float
    """The warmest a charge by the compressor alone makes it."""
    share: float
    draw_kw: float
    floor: float | None = None
    """The protection floor, °C."""
    least: float | None = None
    """Where the pump starts its own charges, for a tank that can't be held off: the plan
    can't let it fall below."""
    deadlines: tuple[Deadline, ...] = ()
    room_c: float = 20.0


@dataclass(frozen=True)
class Pool:
    scope: str
    temp: float
    a: float
    """K an hour of the compressor's full heating gives it."""
    b: float
    """The share of its lead over outdoors it loses an hour."""
    bands: tuple[Band, ...]
    """Per slot."""
    draw_kw: float


@dataclass(frozen=True)
class Addition:
    kw: float
    """The most it gives, kW."""
    policy: str = "pump"
    """pump, when_needed, not_when_expensive or limit."""
    limit_kw: float | None = None


@dataclass(frozen=True)
class Problem:
    slots: tuple[Slot, ...]
    systems: tuple[System, ...] = ()
    tanks: tuple[Tank, ...] = ()
    pools: tuple[Pool, ...] = ()
    addition: Addition | None = None
    ranking: tuple[str, ...] = (
        "comfort_low",
        "must_deadlines",
        "should_deadlines",
        "comfort_high",
        "power_peak",
    )
    slider: float | None = None
    """None: no cost stance, so nothing is moved by price."""

    @property
    def hours(self) -> int:
        return len(self.slots) // 4


@dataclass
class SystemPlan:
    scope: str
    offset: list[float] = field(default_factory=list)
    """Per hour, as planned: not yet on the lever's steps."""
    water: list[float] = field(default_factory=list)
    room: list[float] = field(default_factory=list)
    """The rooms' mean, per hour, at its end."""
    heat_kw: list[float] = field(default_factory=list)
    addition_kw: list[float] = field(default_factory=list)
    share: list[float] = field(default_factory=list)
    """Per slot: the share of it the compressor heats this system."""


@dataclass
class TankPlan:
    scope: str
    share: list[float] = field(default_factory=list)
    """Per slot: the share of it the compressor charges the tank."""
    level: list[float] = field(default_factory=list)
    """At the end of each slot."""


@dataclass
class PoolPlan:
    scope: str
    share: list[float] = field(default_factory=list)
    temp: list[float] = field(default_factory=list)


@dataclass
class Plan:
    ok: bool
    why: str = ""
    systems: dict[str, SystemPlan] = field(default_factory=dict)
    tanks: dict[str, TankPlan] = field(default_factory=dict)
    pools: dict[str, PoolPlan] = field(default_factory=dict)
    kw: list[float] = field(default_factory=list)
    """The pump's electricity, per slot."""
    cost: float = 0.0
    """What the electricity over the horizon costs, where its price is known."""
    stages: dict[str, float] = field(default_factory=dict)
    """What each stage left short, in its own unit: °C·h, °C, kWh."""
    widened: list[str] = field(default_factory=list)
    seconds: float = 0.0
