"""The installation's settings, as the web UI and an import set them.

Each kind of setting is a model; the database keeps one document per kind and id. A
setting never holds a secret, only the name of its entry in the secrets file, so no
settings dump, export or log line can carry one.
"""

from typing import Annotated, ClassVar, Literal, Self
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    StringConstraints,
    ValidationError,
    model_validator,
)

Port = Annotated[int, Field(ge=1, le=65535)]

SecretName = Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9_.:-]*$")]
"""The name of an entry in the secrets file, such as `mqtt.password`."""

Id = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_.-]*$", max_length=64)]


class Setting(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: ClassVar[str]


class Location(Setting):
    """For forecasts and the sun. One per installation."""

    kind = "location"

    latitude: Annotated[float, Field(ge=-90, le=90)]
    longitude: Annotated[float, Field(ge=-180, le=180)]
    timezone: str
    """The house's time zone, such as Europe/Stockholm."""

    @model_validator(mode="after")
    def _known_zone(self) -> Self:
        try:
            ZoneInfo(self.timezone)
        except (ZoneInfoNotFoundError, ValueError) as e:
            raise ValueError(f"unknown time zone {self.timezone!r}") from e
        return self


class Mqtt(Setting):
    """The MQTT broker. One per installation."""

    kind = "mqtt"

    enabled: bool = True
    host: str
    port: Port = 1883
    username: str | None = None
    password: SecretName | None = None
    tls: bool = False
    discovery: bool = True
    """Publish Home Assistant discovery messages."""


class NibeGateway(BaseModel):
    """A Nibe pump's gateway: the settings of a `nibe` plugin instance."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    host: str
    protocol: Literal["nibegw", "thermaestro-gw"] = "nibegw"
    """Plain NibeGW, or the Thermaestro gateway protocol on the control port."""
    read_port: Port = 9999
    write_port: Port = 10000
    control_port: Port = 10090
    local_port: Annotated[int, Field(ge=0, le=65535)] = 0
    """Where Thermaestro listens for the gateway's datagrams; 0 picks a free port. A
    gateway that sends to a fixed port needs it set."""
    psk: SecretName | None = None
    """The control port's pre-shared key: 64 hex digits in the secrets file."""
    model: str | None = None
    """The pump model as the register map names it (`F1245`). Without it, the model is
    taken from the product information the pump sends every 15 s."""

    @model_validator(mode="after")
    def _protocol_has_key(self) -> Self:
        if self.protocol == "thermaestro-gw" and self.psk is None:
            raise ValueError("the Thermaestro gateway protocol needs its pre-shared key")
        return self


class HomeAssistant(BaseModel):
    """A Home Assistant: the settings of a `homeassistant` plugin instance."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    url: Annotated[str, StringConstraints(pattern=r"^https?://[^\s/]+(:\d+)?/?$")]
    """Where it answers, such as http://homeassistant.local:8123."""
    token: SecretName
    """A long-lived access token made in Home Assistant, in the secrets file."""
    entities: tuple[Annotated[str, StringConstraints(pattern=r"^[a-z_]+\.[a-z0-9_]+$")], ...] = ()
    """The entities to read; nothing else is."""


class Tibber(BaseModel):
    """A Tibber account: the settings of a `tibber` plugin instance."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    token: SecretName
    """A personal access token from developer.tibber.com, in the secrets file."""
    home: str | None = None
    """Tibber's id of the home whose prices to take; None: the first with a contract."""


Zone = Annotated[str, StringConstraints(pattern=r"^[A-Z]{2}[A-Z0-9-]{0,8}$")]


class EntsoE(BaseModel):
    """A bidding zone's day-ahead prices from the ENTSO-E Transparency Platform: the
    settings of an `entsoe` plugin instance."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    token: SecretName
    """The user's own security token for the platform's API, in the secrets file."""
    zone: Zone
    """The bidding zone, such as SE3 or DE-LU."""
    currency: Annotated[str, StringConstraints(pattern=r"^[A-Z]{3}$")] = "EUR"
    """The currency to give prices in; other than EUR, converted at the ECB's rates."""


PLUGIN_SETTINGS: dict[str, type[BaseModel]] = {
    "nibe": NibeGateway,
    "homeassistant": HomeAssistant,
    "tibber": Tibber,
    "entsoe": EntsoE,
}
"""The settings model of each plugin that has one, checked whenever settings load."""


class Plugin(Setting):
    """A plugin instance: which plugin, and its settings. The id tells instances apart."""

    kind = "plugin"

    plugin: Annotated[str, StringConstraints(pattern=r"^[a-z0-9_]+$")]
    enabled: bool = True
    settings: dict[str, JsonValue] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _settings_fit_the_plugin(self) -> Self:
        model = PLUGIN_SETTINGS.get(self.plugin)
        if model is None:
            return self
        try:
            model.model_validate(self.settings)
        except ValidationError as e:
            problems = (
                f"settings.{'.'.join(str(p) for p in err['loc'])}: {err['msg']}"
                for err in e.errors(include_input=False, include_url=False)
            )
            raise ValueError("; ".join(problems)) from e
        return self

    def typed[M: BaseModel](self, model: type[M]) -> M:
        return model.model_validate(self.settings)


Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=64)]

PointRef = Annotated[
    str, StringConstraints(pattern=r"^[A-Za-z0-9_.-]{1,64}:[A-Za-z0-9_.{}=,#/-]+$")
]
"""A point of a plugin instance, `<instance>:<path>`, such as `pump:hp1/outdoor.temp` or
`ha:sensor.bedroom_temperature`."""


class Sensor(Setting):
    """A sensor from outside the pump's own logic: an MQTT topic, or a point a plugin
    offers (a Home Assistant entity, or the pump's outdoor sensor used as a reference)."""

    kind = "sensor"

    name: Name
    source: Literal["mqtt", "point"]
    topic: str | None = None
    json_key: str | None = None
    """For a topic carrying JSON: the field with the value, dotted for a nested one
    (`temperature`, `state.temperature`). None: the payload is the number itself."""
    point: PointRef | None = None
    quantity: Annotated[str, StringConstraints(pattern=r"^[a-z0-9_.]{1,64}$")] = "temperature"
    """Its Home Assistant device class, or a room point (`heat_demand`, `setpoint`)."""
    placement: Literal["room", "outdoor", "other"] = "room"
    room: Id | None = None
    reference: bool = False
    """The room's reference for its quantity: used alone instead of the mean of the
    room's sensors (a sensor above a radiator isn't representative; one on an inner wall
    is)."""
    freshness_s: Annotated[float, Field(gt=0)] | None = None
    """How old a value may get before it's stale; None for Thermaestro's default. A
    room sensor is never 'never stale', since a stale one restores the pump's settings."""
    calibration_offset: float = 0.0

    @model_validator(mode="after")
    def _names_its_source(self) -> Self:
        if self.source == "mqtt" and not self.topic:
            raise ValueError("an MQTT sensor names its topic")
        if self.source == "point" and not self.point:
            raise ValueError("a sensor from another plugin names its point")
        if self.placement != "room" and (self.room or self.reference):
            raise ValueError("only a room sensor belongs to a room")
        return self


class Room(Setting):
    """A room of a climate system, with its own sensors, and maybe a thermostat or valves
    of its own."""

    kind = "room"

    name: Name
    climate_system: PointRef | None = None
    """The climate system's node, `<instance>:<path>`, such as `pump:hp1/cs1`."""
    own_device: Literal[
        "unknown",
        "none",
        "simple_thermostat",
        "smart_thermostat",
        "radiator_valves",
        "zone_controller",
    ] = "unknown"
    """What controls the room besides the pump: a simple on/off thermostat (no interface),
    a smart one or valves with an interface, a zoning controller, or nothing."""


class Outdoor(Setting):
    """Which sensor is the outdoor reference for each quantity. One per installation;
    without one for temperature, the pump's own outdoor sensor is it."""

    kind = "outdoor"

    references: dict[Annotated[str, StringConstraints(pattern=r"^[a-z0-9_.]{1,64}$")], Id] = Field(
        default_factory=dict
    )
    """Quantity to sensor id."""


class Names(Setting):
    """The household's own names for points and nodes, in its own language, replacing the
    built-in ones for everyone. One per installation."""

    kind = "names"

    names: dict[PointRef, Name] = Field(default_factory=dict)


class Display(Setting):
    """How the household wants points shown, for everyone. One per installation."""

    kind = "display"

    categories: dict[PointRef, Literal["primary", "config", "diagnostic"]] = Field(
        default_factory=dict
    )
    """A point moved to another category than its plugin gave it."""
    pinned: tuple[PointRef, ...] = ()
    """Points shown on the overview's cards, in the order pinned."""


SeriesRef = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_.-]{1,64}:\S{1,128}$")]
"""A series of a plugin instance, `<instance>:<series>`, such as `entsoe:spot`."""


class PriceLayer(Setting):
    """One layer of the import price: a series a plugin offers, or a fixed amount."""

    kind = "price.layer"

    role: Annotated[str, StringConstraints(pattern=r"^[a-z_]+(\.[a-z_]+)*$")]
    source: Literal["series", "fixed"]
    plugin: Id | None = None
    """The plugin instance offering the series."""
    series: str | None = None
    fallbacks: tuple[SeriesRef, ...] = ()
    """Series of the same kind that stand in, in order, where this one has no price: a
    second source of the spot price, for a day the first one doesn't publish."""
    value: float | None = None
    unit: str
    vat: Literal["incl", "excl"]

    @model_validator(mode="after")
    def _has_its_source(self) -> Self:
        if self.source == "series" and not (self.plugin and self.series):
            raise ValueError("a series layer names the plugin and the series")
        if self.source == "fixed" and self.value is None:
            raise ValueError("a fixed layer gives its value")
        if self.source == "fixed" and self.fallbacks:
            raise ValueError("only a series layer has fallbacks")
        return self


class Vat(Setting):
    """VAT: a rate, and the layers it is charged on. One per installation."""

    kind = "price.vat"

    rate: Annotated[float, Field(ge=0, le=1)]
    applies_to: tuple[str, ...]


SETTINGS: dict[str, type[Setting]] = {
    m.kind: m
    for m in (Location, Mqtt, Plugin, Sensor, Room, Outdoor, Names, Display, PriceLayer, Vat)
}
