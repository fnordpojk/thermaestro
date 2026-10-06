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
    """The control port's pre-shared key."""

    @model_validator(mode="after")
    def _protocol_has_key(self) -> Self:
        if self.protocol == "thermaestro-gw" and self.psk is None:
            raise ValueError("the Thermaestro gateway protocol needs its pre-shared key")
        return self


PLUGIN_SETTINGS: dict[str, type[BaseModel]] = {"nibe": NibeGateway}
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


class Sensor(Setting):
    """A sensor from outside the pump: an MQTT topic, or a point another plugin offers."""

    kind = "sensor"

    name: str
    source: Literal["mqtt", "point"]
    topic: str | None = None
    point: str | None = None
    quantity: str = "temperature"
    """Its Home Assistant device class."""
    freshness_s: Annotated[float, Field(gt=0)] | None = None
    """How old a value may get before it's stale; None for Thermaestro's default. A
    room sensor is never 'never stale', since a stale one restores the pump's settings."""
    calibration_offset: float = 0.0
    climate_systems: tuple[str, ...] = ()
    """The climate systems it is a room sensor of; several sensors may serve one."""

    @model_validator(mode="after")
    def _names_its_source(self) -> Self:
        if self.source == "mqtt" and not self.topic:
            raise ValueError("an MQTT sensor names its topic")
        if self.source == "point" and not self.point:
            raise ValueError("a sensor from another plugin names its point")
        return self


class PriceLayer(Setting):
    """One layer of the import price: a series a plugin offers, or a fixed amount."""

    kind = "price.layer"

    role: Annotated[str, StringConstraints(pattern=r"^[a-z_]+(\.[a-z_]+)*$")]
    source: Literal["series", "fixed"]
    plugin: Id | None = None
    """The plugin instance offering the series."""
    series: str | None = None
    value: float | None = None
    unit: str
    vat: Literal["incl", "excl"]

    @model_validator(mode="after")
    def _has_its_source(self) -> Self:
        if self.source == "series" and not (self.plugin and self.series):
            raise ValueError("a series layer names the plugin and the series")
        if self.source == "fixed" and self.value is None:
            raise ValueError("a fixed layer gives its value")
        return self


class Vat(Setting):
    """VAT: a rate, and the layers it is charged on. One per installation."""

    kind = "price.vat"

    rate: Annotated[float, Field(ge=0, le=1)]
    applies_to: tuple[str, ...]


SETTINGS: dict[str, type[Setting]] = {
    m.kind: m for m in (Location, Mqtt, Plugin, Sensor, PriceLayer, Vat)
}
