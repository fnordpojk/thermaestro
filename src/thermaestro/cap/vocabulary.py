"""The standard names for points and levers, in heat-pump terms.

Standard names make points and levers findable; a device's metadata makes them usable, so
a device states the shape of each lever (enum values, curve form, speed unit) rather than
the vocabulary forcing one. Anything outside the vocabulary goes under the plugin's own
namespace, `x.<plugin>.<name>`: the core logs and shows it, but never plans on it.

Names are `area.quantity`. On a node of the area's own kind the area may be left out, so
`dhw.block` on a hot-water tank is written `hp1/dhw/block`. A name may carry qualifiers
in braces, `heat.produced{purpose=dhw}`, and a suffix telling several of the same apart,
`temperature#2`.

Sensor quantities follow Home Assistant's sensor device classes, with the unit
Thermaestro stores; plugins convert.
"""

import re
from dataclasses import dataclass

from .model import LeverKind

AREA_OF_KIND = {
    "dhw_tank": "dhw",
    "pool": "pool",
    "addition": "addition",
    "compressor": "compressor",
    "ventilation": "ventilation",
}

UNIT, CS, DHW, ROOM = "unit", "climate_system", "dhw_tank", "room"


@dataclass(frozen=True, slots=True)
class PointName:
    unit: str | None
    """None: the device declares it (a compressor's speed in Hz, rps, rpm, % or gear)."""
    on: frozenset[str]
    """The node kinds it belongs on."""
    values: tuple[str, ...] = ()
    """For an enum, its standard values."""


@dataclass(frozen=True, slots=True)
class LeverName:
    kinds: frozenset[LeverKind]
    user_only: bool = False
    """The core never uses it by itself, only on a user's explicit action."""


def _p(unit: str | None, *on: str, values: tuple[str, ...] = ()) -> PointName:
    return PointName(unit, frozenset(on), values)


def _l(*kinds: LeverKind, user_only: bool = False) -> LeverName:
    return LeverName(frozenset(kinds), user_only)


POINTS: dict[str, PointName] = {
    "outdoor.temp": _p("degC", UNIT),
    "supply.temp": _p("degC", CS, UNIT),
    "return.temp": _p("degC", CS, UNIT),
    "temp.top": _p("degC", DHW),
    "temp.mid": _p("degC", DHW),
    "temp.charge": _p("degC", DHW),
    "temp.start": _p("degC", DHW, "pool"),
    "temp.stop": _p("degC", DHW, "pool"),
    "pool.temp": _p("degC", "pool"),
    "room.temp": _p("degC", ROOM, CS),
    "brine.in.temp": _p("degC", "brine_circuit"),
    "brine.out.temp": _p("degC", "brine_circuit"),
    "brine.delta_t": _p("K", "brine_circuit"),
    "heat.extracted.power": _p("kW", "brine_circuit"),
    "heat.extracted": _p("kWh", "brine_circuit"),
    "demand": _p(None, UNIT, values=("idle", "dhw", "heating", "pool", "cooling", "transfer")),
    "diverter": _p(None, UNIT, values=("heating", "dhw")),
    "state": _p(None, "compressor", values=("stopped", "starting", "running", "stopping")),
    "speed": _p(None, "compressor"),
    "emitter": _p(None, CS, values=("radiators", "floor", "radiators_and_floor", "fan_coils")),
    "pump.state": _p(None, CS, "brine_circuit"),
    "pump.speed": _p(None, CS, "brine_circuit"),
    "power": _p(None, "addition"),
    "degree_minutes": _p("degC.min", UNIT),
    "heat.produced": _p("kWh", UNIT),
    "elec.used": _p("kWh", UNIT),
    "elec.power": _p("kW", UNIT, "meter"),
    "grid.import.power": _p("kW", "meter"),
    "grid.export.power": _p("kW", "meter"),
    "alarm": _p(None, UNIT),
    "airflow": _p("m3/h", "ventilation"),
    "fan.speed": _p("%", "ventilation"),
    "exhaust.temp": _p("degC", "ventilation"),
    # what a room's own thermostat or valves report
    "heat_demand": _p("%", ROOM),
    "zone.open": _p(None, ROOM),
    "setpoint": _p("degC", ROOM),
    "window.open": _p(None, ROOM),
}

QUANTITIES: dict[str, str] = {
    "temperature": "degC",
    "humidity": "%",
    "absolute_humidity": "g/m3",
    "carbon_dioxide": "ppm",
    "volatile_organic_compounds": "ug/m3",
    "volatile_organic_compounds_parts": "ppb",
    "pm25": "ug/m3",
    "pm10": "ug/m3",
    "atmospheric_pressure": "hPa",
    "illuminance": "lx",
    "irradiance": "W/m2",
    "power": "W",
    "energy": "kWh",
    "volume_flow_rate": "L/min",
    "water": "L",
}
"""Sensor quantities on any node, by Home Assistant device class, with the stored unit."""

DERIVED: dict[str, str] = {"dew_point": "degC", "absolute_humidity": "g/m3"}
"""Computed by the core from other points, never more trustworthy than their inputs."""

WEATHER: dict[str, str] = {
    "temperature": "degC",
    "temperature.p10": "degC",
    "temperature.p90": "degC",
    "dew_point": "degC",
    "relative_humidity": "%",
    "cloud_cover": "%",
    "irradiance.global": "W/m2",
    "irradiance.direct_normal": "W/m2",
    "irradiance.diffuse": "W/m2",
    "wind_speed": "m/s",
    "wind_gust": "m/s",
    "wind_direction": "deg",
    "precipitation": "mm",
    "pressure": "hPa",
}
"""Forecast quantities, with the unit a forecast series gives them in. A value for an
instant (temperature, humidity, cloud cover, wind, pressure) is its interval's start;
irradiance is the mean over the interval, precipitation the amount in it, and a gust
the strongest the provider gives for it. Wind blows from the direction given, clockwise
from north; pressure is at sea level."""

STATES = frozenset({"window", "door", "opening", "presence", "occupancy", "motion", "moisture"})
"""On/off states, by Home Assistant binary sensor device class."""

LEVERS: dict[str, LeverName] = {
    "heating.offset": _l("setting"),
    "heating.curve": _l("setting"),
    "heating.flow_setpoint": _l("setting", "feed"),
    "room.setpoint": _l("setting"),
    "ventilation.fan_speed": _l("setting"),
    "ventilation.boost": _l("hold"),
    "dhw.mode": _l("setting"),
    "dhw.start_temp": _l("setting"),
    "dhw.stop_temp": _l("setting"),
    "dhw.block": _l("hold"),
    "dhw.boost_once": _l("trigger"),
    "compressor.block": _l("hold"),
    "addition.block": _l("hold"),
    "heating.block": _l("hold"),
    "addition.policy": _l("setting"),
    "addition.stop_temp": _l("setting"),
    "addition.max_power": _l("setting"),
    "pool.start_temp": _l("setting"),
    "pool.stop_temp": _l("setting"),
    "pool.block": _l("hold"),
    "operating_mode": _l("setting"),
    "grid.sg_state": _l("hold"),
    "power.limit": _l("setting", "hold"),
    "alarm.reset": _l("trigger", user_only=True),
}

_FEED = re.compile(r"^[a-z_.]+_input$")
"""A value the device lacks or should use instead: `room.temp_input`, `outdoor.temp_input`,
`ventilation.airflow_input`."""

_SUFFIXES = re.compile(r"(\{[^}]*\})?(#[0-9]+)?$")


def bare(name: str) -> str:
    """The name without its qualifiers and numbering suffix."""
    return _SUFFIXES.sub("", name, count=1)


def _full_names(node_kind: str, name: str) -> list[str]:
    name = bare(name)
    area = AREA_OF_KIND.get(node_kind)
    return [name, f"{area}.{name}"] if area else [name]


def point(node_kind: str, name: str) -> PointName | None:
    """The standard point a name on a node of this kind stands for, if any."""
    for full in _full_names(node_kind, name):
        if full in POINTS:
            return POINTS[full]
        if full in QUANTITIES:
            return PointName(QUANTITIES[full], frozenset())
        if full in DERIVED:
            return PointName(DERIVED[full], frozenset())
        if full in STATES:
            return PointName(None, frozenset())
    return None


def lever(node_kind: str, name: str) -> LeverName | None:
    """The standard lever a name on a node of this kind stands for, if any."""
    for full in _full_names(node_kind, name):
        if full in LEVERS:
            return LEVERS[full]
        if _FEED.match(full):
            return _l("feed")
    return None


def is_vendor(name: str, plugin: str) -> bool:
    return name.startswith(f"x.{plugin}.")
