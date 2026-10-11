"""The messages between the core and a plugin.

In process they are passed as objects; over a socket, as one JSON object per line. Every
request carries an `id` the core chooses, and everything that follows from it carries the
same `id`.
"""

from typing import Annotated, Literal, Self

from pydantic import AwareDatetime, ConfigDict, Field, TypeAdapter, model_validator

from .model import (
    Envelope,
    Interval,
    Lever,
    Model,
    Node,
    Path,
    PluginName,
    Point,
    Provider,
    RuleRecord,
    SeriesInfo,
    Value,
)

PROTOCOL = "thermaestro-cap"
VERSION = "0.3"
"""0.2 added the capability register: `Described.provider` and the forecast fields of
`SeriesInfo`, announced by the `forecast` feature. 0.3 added `write`, a person's change of
one of the device's own settings, which a plugin offers with the `write` feature. Older
peers still talk to newer ones."""
FEATURES = ("subscribe", "forecast", "write")
"""What this side of the protocol understands."""

RequestId = Annotated[int, Field(ge=0)]


def major(version: str) -> str:
    return version.split(".", 1)[0]


class Hello(Model):
    """Opens a connection: the core says it first, and the plugin answers with its own.
    Major versions must match; minor differences are handled by `features`, of which the
    core uses only those both list."""

    type: Literal["hello"] = "hello"
    id: RequestId
    protocol: str
    version: Annotated[str, Field(pattern=r"^\d+\.\d+$")]
    role: Literal["core", "plugin"]
    plugin: PluginName | None = None
    plugin_version: str | None = None
    features: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _plugin_says_who_it_is(self) -> Self:
        if self.role == "plugin" and (self.plugin is None or self.plugin_version is None):
            raise ValueError("a plugin's hello names the plugin and its version")
        return self


class Describe(Model):
    type: Literal["describe"] = "describe"
    id: RequestId


class Described(Model):
    """The device tree, every point and lever, and the series. The answer to `describe`
    carries its `id` and is complete; sent unasked, it carries the changed parts only."""

    type: Literal["described"] = "described"
    id: RequestId | None = None
    complete: bool = True
    nodes: tuple[Node, ...] = ()
    points: tuple[Point, ...] = ()
    levers: tuple[Lever, ...] = ()
    series: tuple[SeriesInfo, ...] = ()
    provider: Provider | None = None
    """Who the series come from and on what terms (protocol 0.2)."""
    removed: tuple[Path, ...] = ()


class Read(Model):
    """With `after`, only values sampled after that time: what a fresh read after a write
    needs. A route that can't tell answers with `t_observed` null."""

    type: Literal["read"] = "read"
    id: RequestId
    points: Annotated[tuple[Path, ...], Field(min_length=1)]
    after: AwareDatetime | None = None


class Values(Model):
    type: Literal["values"] = "values"
    id: RequestId
    values: tuple[Envelope, ...]


class Subscribe(Model):
    """Updates on points, from pushes where the route has them and polls otherwise."""

    type: Literal["subscribe"] = "subscribe"
    id: RequestId
    points: Annotated[tuple[Path, ...], Field(min_length=1)]
    min_interval_s: Annotated[float, Field(ge=0)] = 0.0
    on_change: bool = True


class Unsubscribe(Model):
    """Ends the subscription whose `id` it carries."""

    type: Literal["unsubscribe"] = "unsubscribe"
    id: RequestId


class Update(Model):
    type: Literal["update"] = "update"
    id: RequestId
    """The subscription's."""
    values: tuple[Envelope, ...]


Op = Literal["set", "engage", "release", "fire", "cancel", "feed", "renew"]


class Act(Model):
    type: Literal["act"] = "act"
    id: RequestId
    lever: Path
    op: Op
    params: dict[str, Value] = Field(default_factory=dict)


class Write(Model):
    """A person's change of one of the device's own settings, outside the levers: a point
    under `x.<plugin>`, described or not, in the units its reads give (protocol 0.3, the
    `write` feature). Answered with fates, as `act` is. The core sends it only for a person,
    never for the planner."""

    type: Literal["write"] = "write"
    id: RequestId
    point: Path
    value: float


FateStage = Literal["queued", "sent", "device_accepted", "device_refused", "dropped", "unknown"]
FINAL_STAGES: frozenset[FateStage] = frozenset(
    {"device_accepted", "device_refused", "dropped", "unknown"}
)


class Fate(Model):
    """What became of an `act`, as far as the route can know. Accepted isn't applied:
    whether a change took is the core's to decide."""

    type: Literal["fate"] = "fate"
    id: RequestId
    stage: FateStage
    t: AwareDatetime
    detail: str | None = None


class Health(Model):
    type: Literal["health"] = "health"
    t: AwareDatetime
    unit: Path | None = None
    """The unit it's about; None for the whole plugin."""
    state: Literal["up", "down", "contended"]
    last_traffic: AwareDatetime | None = None
    counters: dict[str, int] = Field(default_factory=dict)
    queue_depth: Annotated[int, Field(ge=0)] | None = None
    silence_s: Annotated[float, Field(ge=0)] | None = None
    needs_user_action: str | None = None
    """Why the route stops until a person acts: an expired login, a new policy to accept."""
    stale: tuple[str, ...] = ()
    """Series whose expected publication hasn't arrived."""


class DeviceEvent(Model):
    type: Literal["device_event"] = "device_event"
    t: AwareDatetime
    unit: Path
    code: str
    text: str
    active: bool = True


class ForeignWrite(Model):
    """Another client wrote a datapoint, where the route can see it."""

    type: Literal["foreign_write"] = "foreign_write"
    t: AwareDatetime
    unit: Path
    datapoint: str
    value: int | float | str | None = None
    detail: str | None = None


class SeriesGet(Model):
    model_config = ConfigDict(serialize_by_alias=True, validate_by_name=True)

    type: Literal["series.get"] = "series.get"
    id: RequestId
    series: str
    start: AwareDatetime = Field(alias="from")
    end: AwareDatetime = Field(alias="to")


class SeriesData(Model):
    type: Literal["series.data"] = "series.data"
    id: RequestId
    series: str
    intervals: tuple[Interval, ...]
    known_until: AwareDatetime | None = None


class SeriesSubscribe(Model):
    type: Literal["series.subscribe"] = "series.subscribe"
    id: RequestId
    series: str


class SeriesUpdate(Model):
    """New intervals, or a revision of published ones."""

    type: Literal["series.update"] = "series.update"
    id: RequestId
    """The subscription's."""
    series: str
    intervals: tuple[Interval, ...]


class RulesGet(Model):
    type: Literal["rules.get"] = "rules.get"
    id: RequestId
    scope: str


class Rules(Model):
    type: Literal["rules"] = "rules"
    id: RequestId
    rules: tuple[RuleRecord, ...]


class Error(Model):
    """A plugin's answer to a request it can't serve. Requests with their own way to fail
    don't use it: a read of an unknown point answers with quality unknown, and an act on
    an unknown lever ends with fate dropped."""

    type: Literal["error"] = "error"
    id: RequestId | None
    """The request's; None for a line too broken to have one."""
    code: Literal["version", "unsupported", "invalid"]
    detail: str | None = None


class Auth(Model):
    """Over TCP, the plugin's first line. Without it, or with the wrong token, the core
    closes the connection without answering."""

    type: Literal["auth"] = "auth"
    token: str


Message = Annotated[
    Hello
    | Describe
    | Described
    | Read
    | Values
    | Subscribe
    | Unsubscribe
    | Update
    | Act
    | Write
    | Fate
    | Health
    | DeviceEvent
    | ForeignWrite
    | SeriesGet
    | SeriesData
    | SeriesSubscribe
    | SeriesUpdate
    | RulesGet
    | Rules
    | Error
    | Auth,
    Field(discriminator="type"),
]

MESSAGES: TypeAdapter[Message] = TypeAdapter(Message)

TO_PLUGIN = frozenset(
    {
        "hello",
        "describe",
        "read",
        "subscribe",
        "unsubscribe",
        "act",
        "write",
        "series.get",
        "series.subscribe",
        "rules.get",
    }
)
TO_CORE = frozenset(
    {
        "hello",
        "described",
        "values",
        "update",
        "fate",
        "health",
        "device_event",
        "foreign_write",
        "series.data",
        "series.update",
        "rules",
        "error",
        "auth",
    }
)
TYPES = TO_PLUGIN | TO_CORE
