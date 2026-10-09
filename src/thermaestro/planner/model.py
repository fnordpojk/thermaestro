"""What the planner sees in one round, what it decides, and what it keeps between rounds.

The rules (`rules.py`) are pure: a `Situation` in, decisions out, so each can be tested
without a pump. The planner (`service.py`) gathers the situation from the running core,
asks the executor for each decision, and follows up on what became of it.
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta, tzinfo

from ..cap.messages import Op
from ..cap.model import Value
from ..intents import Capabilities, Deadline, InForce

SLOT = timedelta(minutes=15)

RANK_SLIDER = 7
"""What price shifting serves: the last stage, after every ranked goal."""


@dataclass(frozen=True)
class Price:
    start: datetime
    value: float


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


@dataclass
class Memory:
    """What the planner keeps between rounds, in memory: rebuilt after a restart."""

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


@dataclass(frozen=True)
class Decision:
    lever: str
    op: Op | str
    """An operation, or `restore`: put it back as it was found."""
    params: dict[str, Value] = field(default_factory=dict)
    rank: int = RANK_SLIDER
    """What it serves: 1 a protection floor, then the ranking's place (2 to 6), 7 price."""
    reason: str = ""
