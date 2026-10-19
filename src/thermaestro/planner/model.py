"""What the planner sees in one round, what it decides, and what it keeps between rounds.

The rules (`rules.py`) are pure: a `Situation` in, decisions out, so each can be tested
without a pump. The planner (`service.py`) gathers the situation from the running core,
asks the executor for each decision, and follows up on what became of it.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta, tzinfo
from typing import Any

from ..cap.messages import Op
from ..cap.model import Value
from ..core.gridrules import GridLimit
from ..intents import Capabilities, Deadline, InForce

SLOT = timedelta(minutes=15)

RANK_SLIDER = 7
"""What price shifting serves: the last stage, after every ranked goal."""


@dataclass(frozen=True)
class Price:
    start: datetime
    value: float
    estimated: bool = False
    """Not published yet: from the same quarter of recent weeks. The rules don't use it."""


@dataclass(frozen=True)
class RoomReading:
    scope: str
    """`room:<id>`."""
    system: str
    """Its climate system's node: `pump:hp1/cs1`."""
    temp: float | None
    """None where the room has no good value: every sensor in it stale or silent."""
    age_s: float
    limit_s: float
    """How long its sensors may stay quiet before they are stale."""
    name: str = ""
    """What the household calls it."""
    airing: bool = False
    """A window open, or a sudden drop that looks like one: heat isn't raised for it."""

    @property
    def weight(self) -> float:
        """How much the reading counts: fully for half an hour, then less as it ages, and
        nothing once it is stale."""
        if self.temp is None:
            return 0.0
        fresh = min(1800.0, self.limit_s / 2)
        if self.age_s <= fresh:
            return 1.0
        return max(0.0, 1.0 - (self.age_s - fresh) / max(1.0, self.limit_s - fresh))


@dataclass(frozen=True)
class LeverState:
    ref: str
    """`<instance>:<path>`."""
    kind: str
    """setting, hold or trigger."""
    low: float | None = None
    high: float | None = None
    step: float | None = None
    current: Value | None = None
    """What it reads now: what Thermaestro set, or the device's own value."""
    baseline: Value | None = None
    """What it was found at; the device's own value before it is taken over."""
    held: bool = False
    hold_until: datetime | None = None
    """Until when a new value would be refused: the least time between two changes."""

    def fit(self, value: float) -> float:
        """A value the lever takes: within its range, on its steps."""
        if self.step:
            base = self.low or 0.0
            value = base + round((value - base) / self.step) * self.step
        if self.low is not None:
            value = max(self.low, value)
        if self.high is not None:
            value = min(self.high, value)
        return round(value, 3)


@dataclass(frozen=True)
class Tank:
    scope: str
    top: float | None
    """The top's temperature (BT7)."""
    start: float | None
    """The pump's own start temperature, as the household has it."""
    stop: float | None = None
    """Its stop temperature."""
    loss_per_h: float | None = None
    """The learned standing loss, per hour, times how far the tank is above its room; None
    until it and the demand have proved themselves."""
    draws: tuple[float, ...] = ()
    """What the household is expected to draw in each hour from now's, in degrees of the
    tank: with `loss_per_h`, from the learned week."""


@dataclass
class Effect:
    """A change whose effect is waited for: the pump starting a charge."""

    lever: str
    reason: str
    since: datetime
    wait_s: float


@dataclass
class Memory:
    """What the planner keeps between rounds. Kept in the database too, so a restart
    carries on from it: all of it within `FRESH_S`; after a longer stop, what still holds
    (the loop's integral, the optimizer's drift, the periodic increase last seen, how long
    charges take, the boosts asked for, when the compressor stopped, rooms aired, changes
    not kept), not the price holds, a charge under way or effects waited for, which a stop
    has ended."""

    integral: dict[str, float] = field(default_factory=dict)
    """Per climate system: the PI loop's integral, in offset steps."""
    last_round: datetime | None = None
    periodic: dict[str, datetime] = field(default_factory=dict)
    """Per tank: when the pump's periodic increase was last seen."""
    charge_s: dict[str, float] = field(default_factory=dict)
    """Per tank: how long a charge has taken, as observed."""
    blocked: dict[str, bool] = field(default_factory=dict)
    """Per tank: whether price holds its charges off, for the hysteresis."""
    blocked_since: dict[str, datetime] = field(default_factory=dict)
    """Per tank: since when price has held its charges off, for the longest hold."""
    fired: dict[str, datetime] = field(default_factory=dict)
    """Boost-now intents whose charge was asked for, by id: so a restart doesn't ask again."""
    charging_since: dict[str, datetime] = field(default_factory=dict)
    """Per tank: since when the pump has been charging it, as seen."""
    drift: dict[str, float] = field(default_factory=dict)
    """Per climate system: the optimizer's learned correction of the house model, °C an
    hour."""
    watch: dict[str, dict[str, float | None]] = field(default_factory=dict)
    """The watch's timers: when each condition began, each room's band and when it rose."""
    stopped: float | None = None
    """When the compressor last stopped, for the optimizer's guard against short cycles."""
    aired: dict[str, float] = field(default_factory=dict)
    """Rooms seen aired, by point: when."""
    not_kept: dict[str, float] = field(default_factory=dict)
    """Changes the device accepted but didn't keep, by what was asked: until when they
    aren't asked again."""
    effects: list[Effect] = field(default_factory=list)
    """Changes whose effect is waited for."""

    def body(self) -> dict[str, Any]:
        return {
            "integral": self.integral,
            "last_round": self.last_round.isoformat() if self.last_round else None,
            "periodic": {k: v.isoformat() for k, v in self.periodic.items()},
            "charge_s": self.charge_s,
            "blocked": self.blocked,
            "blocked_since": {k: v.isoformat() for k, v in self.blocked_since.items()},
            "fired": {k: v.isoformat() for k, v in self.fired.items()},
            "charging_since": {k: v.isoformat() for k, v in self.charging_since.items()},
            "drift": self.drift,
            "watch": self.watch,
            "stopped": self.stopped,
            "aired": self.aired,
            "not_kept": self.not_kept,
            "effects": [
                {
                    "lever": e.lever,
                    "reason": e.reason,
                    "since": e.since.isoformat(),
                    "wait_s": e.wait_s,
                }
                for e in self.effects
            ],
        }

    @classmethod
    def from_body(cls, body: Mapping[str, Any], *, fresh: bool) -> "Memory":
        """As kept; the price holds and the last round only if it was kept `fresh`."""
        last = body.get("last_round")
        return cls(
            integral={k: float(v) for k, v in (body.get("integral") or {}).items()},
            last_round=datetime.fromisoformat(last) if fresh and last else None,
            periodic={
                k: datetime.fromisoformat(v) for k, v in (body.get("periodic") or {}).items()
            },
            charge_s={k: float(v) for k, v in (body.get("charge_s") or {}).items()},
            blocked={k: bool(v) for k, v in (body.get("blocked") or {}).items()} if fresh else {},
            blocked_since={
                k: datetime.fromisoformat(v) for k, v in (body.get("blocked_since") or {}).items()
            }
            if fresh
            else {},
            fired={k: datetime.fromisoformat(v) for k, v in (body.get("fired") or {}).items()},
            charging_since={
                k: datetime.fromisoformat(v) for k, v in (body.get("charging_since") or {}).items()
            }
            if fresh
            else {},
            drift={k: float(v) for k, v in (body.get("drift") or {}).items()},
            watch=dict(body.get("watch") or {}),
            stopped=body.get("stopped"),
            aired={k: float(v) for k, v in (body.get("aired") or {}).items()},
            not_kept={k: float(v) for k, v in (body.get("not_kept") or {}).items()},
            effects=[
                Effect(e["lever"], e["reason"], datetime.fromisoformat(e["since"]), e["wait_s"])
                for e in body.get("effects") or ()
            ]
            if fresh
            else [],
        )


FRESH_S = 3600.0
"""How recently the planner's memory must have been kept to be taken up whole."""


@dataclass(frozen=True)
class Situation:
    now: datetime
    zone: tzinfo
    force: InForce
    """What the intents ask now."""
    ahead: dict[str, tuple[float | None, float | None]]
    """Per room: its band one response time from now, for pre-heating."""
    deadlines: list[Deadline]
    caps: Capabilities
    prices: list[Price]
    """From about half a day back to the last price known."""
    rooms: list[RoomReading]
    tanks: dict[str, Tank]
    levers: dict[str, LeverState]
    """The levers the planner may ask for, by reference."""
    heating: bool = True
    """Whether the pump's heating stop has heating on."""
    house_kw: float | None = None
    charge_kw: float | None = None
    """What a hot-water charge draws, where learned and proved; else the rules' guess."""
    grid_limit: GridLimit | None = None
    """By a subscribed power, or a power charge in its window."""
    demand: str | None = None
    pools: dict[str, float | None] = field(default_factory=dict)
    """Per pool: its temperature."""
    budget: tuple[int, int] | None = None
    """Writes in the last day, and the soft budget."""
    boost_scopes: frozenset[str] = frozenset()
    """Tanks a "boost now" asks a charge of, not yet given."""
    slot: bool = True
    """A round at the start of a slot. Between slots only hot water is planned: the
    heating, the addition and the pool change at most once a slot."""
    memory: Memory = field(default_factory=Memory)
    response_h: dict[str, float] = field(default_factory=dict)
    """Per climate system, how long its heat takes to show, where learned and proved."""
    lead: dict[str, tuple[int, float, float]] = field(default_factory=dict)
    """Per climate system without a room sensor: offset steps that heat now for the
    weather one response ahead, the outdoor temperature expected then, and the hours
    ahead; where the pump's curve is learned and proved."""


@dataclass(frozen=True)
class Decision:
    lever: str
    op: Op | str
    """An operation, or `restore`: put it back as it was found."""
    params: dict[str, Value] = field(default_factory=dict)
    rank: int = RANK_SLIDER
    """What it serves: 1 a protection floor, then the ranking's place (2 to 6), 7 price."""
    reason: str = ""
    planner: str = "rules"
    """Which planner made it: the rules or the optimizer."""
