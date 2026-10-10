"""The installation's settings, as the web UI and an import set them.

Each kind of setting is a model; the database keeps one document per kind and id. A
setting never holds a secret, only the name of its entry in the secrets file, so no
settings dump, export or log line can carry one.
"""

from datetime import date, time
from typing import Annotated, Any, ClassVar, Literal, Self
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


TopicPath = Annotated[
    str, StringConstraints(pattern=r"^[A-Za-z0-9_-]{1,64}(/[A-Za-z0-9_-]{1,64}){0,3}$")
]
"""MQTT topic levels, without wildcards."""


class Discovery(Setting):
    """Home Assistant MQTT discovery: Thermaestro, and each device it reads, as devices in
    Home Assistant, over the MQTT broker. One per installation."""

    kind = "discovery"

    enabled: bool = False
    prefix: TopicPath = "homeassistant"
    """Home Assistant's discovery prefix, as set in its MQTT integration."""
    base: TopicPath = "thermaestro"
    """Where the values go: `<base>/<id>/…`."""
    id: Annotated[str, StringConstraints(pattern=r"^[a-z0-9]{4,16}$")] | None = None
    """This installation's part of every topic and unique id, made when discovery is
    first switched on, so that two installations can share a broker."""
    sensors: bool = False
    """Also publish each sensor's own values. Home Assistant may have them already."""
    language: Literal["en", "sv", "de"] = "en"
    """The language of the names Home Assistant is given."""


class NibeGateway(BaseModel):
    """A Nibe pump's connection: the settings of a `nibe` plugin instance. A bus-family pump
    is reached through its gateway; an S-series pump answers Modbus TCP itself."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    host: str
    protocol: Literal["nibegw", "thermaestro-gw", "modbus-tcp"] = "nibegw"
    """Plain NibeGW or the Thermaestro gateway protocol on the control port (the bus
    family), or Modbus TCP (the S-series)."""
    modbus_port: Port = 502
    read_port: Port = 9999
    write_port: Port = 10000
    control_port: Port = 10090
    local_port: Annotated[int, Field(ge=0, le=65535)] = 0
    """Where Thermaestro listens for the gateway's datagrams; 0 picks a free port. A
    gateway that sends to a fixed port needs it set."""
    psk: SecretName | None = None
    """The control port's pre-shared key: 64 hex digits in the secrets file."""
    model: str | None = None
    """The pump model as the register map names it (`F1245`, `S1255`). Without it, a
    bus-family pump's model is taken from the product information it sends every 15 s;
    an S-series pump's must be set."""
    brine_flow: Annotated[float, Field(gt=0, le=500)] | None = None
    """The brine flow in liters a minute, at `brine_flow_at` percent of the brine pump's
    speed, as the installation measured or set it. With it, Thermaestro estimates the heat
    taken from the ground; the pump doesn't measure the flow."""
    brine_flow_at: Annotated[int, Field(ge=1, le=100)] = 100
    brine_mix: Literal["ethanol28", "propylene_glycol30", "ethylene_glycol30"] = "ethanol28"

    @model_validator(mode="after")
    def _protocol_has_key(self) -> Self:
        if self.protocol == "thermaestro-gw" and self.psk is None:
            raise ValueError("the Thermaestro gateway protocol needs its pre-shared key")
        if self.protocol == "modbus-tcp" and self.model is None:
            raise ValueError("an S-series pump over Modbus TCP needs its model set")
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
Currency = Annotated[str, StringConstraints(pattern=r"^[A-Z]{3}$")]


class EntsoE(BaseModel):
    """A bidding zone's day-ahead prices from the ENTSO-E Transparency Platform: the
    settings of an `entsoe` plugin instance."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    token: SecretName
    """The user's own security token for the platform's API, in the secrets file."""
    zone: Zone
    """The bidding zone, such as SE3 or DE-LU."""
    currency: Currency = "EUR"
    """The currency to give prices in; other than EUR, converted at the ECB's rates."""


class SpotZone(BaseModel):
    """A bidding zone's day-ahead prices from a source that needs no account: the settings
    of an `energy_charts`, `nordic_sites` or `omie` plugin instance."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    zone: Zone
    currency: Currency = "EUR"
    """The currency to give prices in; other than EUR, converted at the ECB's rates."""


class DanishGrid(BaseModel):
    """A Danish household's grid company and its charge codes in DataHub's price list: the
    settings of an `energidataservice` plugin instance."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    gln: Annotated[str, StringConstraints(pattern=r"^[0-9]{13}$")]
    """The grid company's GLN number."""
    codes: Annotated[
        tuple[Annotated[str, StringConstraints(min_length=1, max_length=32)], ...],
        Field(min_length=1, max_length=6),
    ]
    """Its household tariff's charge code, and any rebate's."""
    company: Annotated[str, StringConstraints(min_length=1, max_length=128)]


class OctopusAgile(BaseModel):
    """Octopus Energy's Agile tariff in Great Britain: the settings of an `octopus_agile`
    plugin instance."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    region: Annotated[str, StringConstraints(pattern=r"^[A-HJ-NP]$")]
    """The grid supply point group, a letter from A to P (there is no I or O): the
    region's prices differ by its network charges."""


Latitude = Annotated[float, Field(ge=-90, le=90)]
Longitude = Annotated[float, Field(ge=-180, le=180)]


class WeatherPoint(BaseModel):
    """Where to forecast for: the settings of a `met_norway` or `smhi` plugin instance,
    copied from the location."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    latitude: Latitude
    longitude: Longitude


class OpenMeteo(WeatherPoint):
    """The settings of an `open_meteo` plugin instance."""

    model: Annotated[str, StringConstraints(pattern=r"^[a-z0-9_]{1,64}$")] = "best_match"
    """Open-Meteo's name of the model or model group to ask; `best_match` lets it combine
    the most suitable ones for the place."""


PLUGIN_SETTINGS: dict[str, type[BaseModel]] = {
    "nibe": NibeGateway,
    "homeassistant": HomeAssistant,
    "tibber": Tibber,
    "entsoe": EntsoE,
    "met_norway": WeatherPoint,
    "smhi": WeatherPoint,
    "open_meteo": OpenMeteo,
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
    """How long the sensor may stay quiet before its value is stale, at most twelve hours;
    None to learn it from how often the sensor reports. A room sensor is never 'never
    stale', since a stale one restores the pump's settings."""
    calibration_offset: float = 0.0

    @model_validator(mode="after")
    def _names_its_source(self) -> Self:
        if self.source == "mqtt" and not self.topic:
            raise ValueError("an MQTT sensor names its topic")
        if self.topic and ("+" in self.topic or "#" in self.topic):
            raise ValueError("an MQTT sensor's topic is one topic, without + or #")
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
    """One layer of the import price: a series a plugin offers, a fixed amount, or a grid
    rule's time-of-use prices."""

    kind = "price.layer"

    role: Annotated[str, StringConstraints(pattern=r"^[a-z_]+(\.[a-z_]+)*$")]
    source: Literal["series", "fixed", "rule"]
    plugin: Id | None = None
    """The plugin instance offering the series."""
    series: str | None = None
    fallbacks: tuple[SeriesRef, ...] = ()
    """Series of the same kind that stand in, in order, where this one has no price: a
    second source of the spot price, for a day the first one doesn't publish."""
    value: float | None = None
    rule: Id | None = None
    """The grid rule whose time-of-use prices the layer holds."""
    unit: str
    vat: Literal["incl", "excl"]

    @model_validator(mode="after")
    def _has_its_source(self) -> Self:
        if self.source == "series" and not (self.plugin and self.series):
            raise ValueError("a series layer names the plugin and the series")
        if self.source == "rule" and not self.rule:
            raise ValueError("a rule layer names the grid rule")
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


Month = Annotated[int, Field(ge=1, le=12)]
DayType = Literal["all", "working_days", "non_working_days", "weekdays", "weekends"]
"""Working days are Monday to Friday except public holidays; non-working days are the
weekends and public holidays."""


class When(BaseModel):
    """When part of a grid rule applies: months, a kind of day, and a time of day. An end
    before the start runs past midnight; no times, all day."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    months: tuple[Month, ...] = ()
    """None: every month."""
    days: DayType = "all"
    start: time | None = None
    end: time | None = None


class Rate(When):
    """A time-of-use price that applies instead of the base price when it holds."""

    price: float


RuleField = Literal["base", "interval_minutes", "peaks", "different_days", "price_per_kw", "kw"]


class GridRule(Setting):
    """A grid company's rule, entered by the household: a time-of-use price per kWh, a
    charge on the power of the highest intervals, or a subscribed power that trips. A rule
    that isn't in force is kept, to show why nothing is charged."""

    kind = "grid.rule"

    type: Literal["tou", "interval_peak", "subscribed_power"]
    owner: Annotated[str, StringConstraints(min_length=1, max_length=128)]
    """The grid company."""
    status: Literal["in_force", "announced", "paused", "withdrawn"] = "in_force"
    valid_from: date | None = None
    valid_to: date | None = None
    """The last day it applies; none: until further notice."""
    clock: Literal["civil", "normal"] = "civil"
    """Civil time with summer time, or normal (standard) time all year."""
    unit: Annotated[str, StringConstraints(min_length=1, max_length=16)] | None = None
    """The currency's per-kWh or per-kW unit, such as SEK/kWh or SEK/kW."""
    vat: Literal["incl", "excl"] = "excl"
    base: float | None = None
    """A time-of-use rule's price per kWh outside its rates."""
    rates: tuple[Rate, ...] = ()
    """A time-of-use rule's other prices; the first that holds applies."""
    window: tuple[When, ...] = ()
    """When an interval counts toward a peak; none: always."""
    interval_minutes: Literal[15, 60] | None = None
    peaks: Annotated[int, Field(ge=1, le=10)] | None = None
    """How many of the period's highest intervals the charge is on, averaged."""
    different_days: bool | None = None
    """Whether those intervals must fall on different days."""
    price_per_kw: float | None = None
    """The charge per kW of the peak, per month."""
    kw: Annotated[float, Field(gt=0, le=1000)] | None = None
    """A subscribed power that trips: never to be exceeded."""
    unknown: tuple[RuleField, ...] = ()
    """What the grid company hasn't said: left empty on purpose, not forgotten."""
    note: Annotated[str, StringConstraints(max_length=500)] | None = None
    """Where the rule is from."""

    @model_validator(mode="after")
    def _says_what_its_type_needs(self) -> Self:
        needs: dict[str, tuple[RuleField, ...]] = {
            "tou": ("base",),
            "interval_peak": ("interval_minutes", "peaks", "different_days", "price_per_kw"),
            "subscribed_power": ("kw",),
        }
        own = needs[self.type]
        for name in own:
            if getattr(self, name) is None and name not in self.unknown:
                raise ValueError(f"a {self.type} rule gives its {name}, or says it isn't known")
        for field, value in (
            ("base", self.base),
            ("rates", self.rates),
            ("window", self.window),
            ("interval_minutes", self.interval_minutes),
            ("peaks", self.peaks),
            ("different_days", self.different_days),
            ("price_per_kw", self.price_per_kw),
            ("kw", self.kw),
        ):
            mine = field in own or (field, self.type) in (
                ("rates", "tou"),
                ("window", "interval_peak"),
            )
            if not mine and value not in (None, ()):
                raise ValueError(f"a {self.type} rule has no {field}")
        if self.type != "subscribed_power" and self.unit is None:
            raise ValueError("a rule with prices gives their unit")
        if self.valid_from and self.valid_to and self.valid_to < self.valid_from:
            raise ValueError("it would end before it starts")
        return self

    def in_force(self, day: date) -> bool:
        return (
            self.status == "in_force"
            and (self.valid_from is None or self.valid_from <= day)
            and (self.valid_to is None or day <= self.valid_to)
        )


WeatherSource = Annotated[
    str, StringConstraints(pattern=r"^[A-Za-z0-9_.-]{1,64}(:[a-z_]+\.[a-z0-9_]+)?$")
]
"""Where forecasts come from: a weather plugin's instance (`met`), or a Home Assistant
instance and one of its weather entities (`ha:weather.forecast_home`)."""

Quantity = Annotated[str, StringConstraints(pattern=r"^[a-z_]+(\.[a-z0-9_]+)*$")]


class WeatherChoice(Setting):
    """Which provider's forecast to use for each quantity. One per installation."""

    kind = "weather"

    main: WeatherSource | None = None
    """The provider for every quantity not chosen otherwise."""
    quantities: dict[Quantity, WeatherSource] = Field(default_factory=dict)
    """A provider chosen for one quantity, instead of the main one."""
    fallbacks: tuple[WeatherSource, ...] = ()
    """Providers that stand in, in order, while the chosen one's forecast is missing or
    stale: Home Assistant's weather, for one."""


class Climate(Setting):
    """The location's climate, for the cold-water estimate: the annual mean temperature
    and the spread of the monthly means. One per installation."""

    kind = "climate"

    annual_mean: Annotated[float, Field(ge=-60, le=60)]
    """°C."""
    monthly_spread: Annotated[float, Field(ge=0, le=80)]
    """The warmest month's mean less the coldest's, K."""
    monthly_means: tuple[float, ...] = ()
    """January to December, °C, where known."""
    source: Literal["open_meteo", "user"]
    period: str | None = None
    """The years averaged, for an archive's numbers."""

    @model_validator(mode="after")
    def _twelve_months(self) -> Self:
        if self.monthly_means and len(self.monthly_means) != 12:
            raise ValueError("monthly means are twelve, January to December")
        return self


Emitter = Literal[
    "unknown",
    "radiators",
    "fan_coils",
    "floor_light",
    "slab",
    "radiators_and_floor_light",
    "radiators_and_slab",
]
"""What gives off a climate system's heat. `floor_light`: loops just under the floor,
in grooved insulation or boards; `slab`: loops cast in a concrete slab, far slower."""


class Home(Setting):
    """What setup asks about the house, and the household's choices that hold for every
    intent. One per installation."""

    kind = "home"

    emitters: dict[PointRef, Emitter] = Field(default_factory=dict)
    """Per climate system's node (`pump:hp1/cs1`): what gives off its heat. How slow a
    system is starts from this, until it is learned."""
    check_emitters: tuple[PointRef, ...] = ()
    """Climate systems whose answer was carried over from an earlier, coarser choice
    ("underfloor heating", now taken as a slab): to be checked; answering again clears it."""
    house: Literal["unknown", "poorly_insulated", "average", "well_insulated", "low_energy"] = (
        "unknown"
    )
    """A rough start for how much heat the house stores and loses, until it is learned."""
    water: Literal["unknown", "municipal", "well"] = "unknown"
    """Where the cold water comes from: a well's stays near the year's mean temperature."""
    holidays: Annotated[str, StringConstraints(pattern=r"^[A-Z]{2}(-[A-Z0-9]{1,3})?$")] | None = (
        None
    )
    """The public holidays' calendar: a country, and maybe a region (`SE`, `DE-BY`)."""
    holidays_as: Annotated[int, Field(ge=0, le=6)] | None = 6
    """The day of the week a public holiday counts as in weekly patterns (0 Monday, 6
    Sunday); None: as the day it falls on."""
    past_deadline: Literal["keep_heating", "stop"] = "keep_heating"
    """After a missed hot-water deadline: keep heating until it is met or the next one is
    due, or stop at the deadline."""


LeverMode = Literal["off", "shadow", "control"]


class Control(Setting):
    """What Thermaestro may change, and how much. One per installation. A lever that isn't
    named is off: Thermaestro reads and plans, and writes nothing to it."""

    kind = "control"

    levers: dict[PointRef, LeverMode] = Field(default_factory=dict)
    """Each lever, `<instance>:<path>`, in shadow (decided and recorded, never sent) or
    in control."""
    confirmed_off: dict[PointRef, tuple[Name, ...]] = Field(default_factory=dict)
    """Per lever, the competing features the household says are switched off, by the
    names the plugin gives them."""
    soft_budget: Annotated[int, Field(ge=1, le=10_000)] = 50
    """Setting writes a day per plugin instance that the planner aims to stay under."""
    guard: Annotated[int, Field(ge=1, le=10_000)] = 200
    """Writes a day per lever after which nothing more is sent: a stop for a writer
    caught in a loop."""
    min_hold_s: Annotated[float, Field(ge=0, le=86_400)] = 900.0
    """The shortest time between two changes of a setting: one price slot."""


class PluginState(Setting):
    """What an in-process plugin keeps across restarts, as it chooses; one per instance,
    under the instance's id. Not a setting anyone changes: the plugin writes it."""

    kind = "plugin_state"

    data: dict[str, Any] = Field(default_factory=dict)


SETTINGS: dict[str, type[Setting]] = {
    m.kind: m
    for m in (
        Location,
        Mqtt,
        Discovery,
        Plugin,
        Sensor,
        Room,
        Outdoor,
        Names,
        Display,
        PriceLayer,
        Vat,
        GridRule,
        WeatherChoice,
        Climate,
        Control,
        Home,
        PluginState,
    )
}
