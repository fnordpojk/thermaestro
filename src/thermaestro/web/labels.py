"""Names for people: the standard point names, their qualifiers and values, and the nodes
they sit on, in the request's language. A point outside the vocabulary shows the label
its plugin gives (a register's title), else its path."""

import re

from . import i18n
from .i18n import mark

POINTS = {
    "outdoor.temp": mark("Outdoor temperature"),
    "supply.temp": mark("Supply temperature"),
    "return.temp": mark("Return temperature"),
    "temp.top": mark("Top temperature"),
    "temp.mid": mark("Middle temperature"),
    "temp.charge": mark("Charging temperature"),
    "room.temp": mark("Room temperature"),
    "brine.in.temp": mark("Brine in"),
    "brine.out.temp": mark("Brine out"),
    "demand": mark("Demand"),
    "diverter": mark("Diverter valve"),
    "state": mark("State"),
    "speed": mark("Speed"),
    "pump.state": mark("Circulation pump"),
    "pump.speed": mark("Circulation pump speed"),
    "power": mark("Power"),
    "degree_minutes": mark("Degree minutes"),
    "heat.produced": mark("Heat produced"),
    "elec.used": mark("Electricity used"),
    "elec.power": mark("Electrical power"),
    "grid.import.power": mark("Power from the grid"),
    "grid.export.power": mark("Power to the grid"),
    "alarm": mark("Alarm"),
    "airflow": mark("Airflow"),
    "fan.speed": mark("Fan speed"),
    "exhaust.temp": mark("Exhaust air temperature"),
}

QUALIFIERS = {
    "dhw": mark("hot water"),
    "heating": mark("heating"),
    "cooling": mark("cooling"),
    "pool": mark("pool"),
    "total": mark("total"),
    "compressor": mark("compressor"),
    "addition": mark("addition"),
}

VALUES = {
    "idle": mark("idle"),
    "dhw": mark("hot water"),
    "heating": mark("heating"),
    "pool": mark("pool"),
    "cooling": mark("cooling"),
    "transfer": mark("transfer"),
    "stopped": mark("stopped"),
    "starting": mark("starting"),
    "running": mark("running"),
    "stopping": mark("stopping"),
    "on": mark("on"),
    "off": mark("off"),
    "yes": mark("yes"),
    "no": mark("no"),
}

NODES = {
    "climate_system": mark("Climate system %(n)s"),
    "dhw_tank": mark("Hot water"),
    "compressor": mark("Compressor"),
    "brine_circuit": mark("Brine circuit"),
    "addition": mark("Addition"),
    "ventilation": mark("Ventilation"),
    "room": mark("Room"),
    "pool": mark("Pool"),
}

_PATH = re.compile(r"^(?P<name>[^{#]+)(?:\{(?P<qualifiers>[^}]*)\})?(?:#(?P<n>\d+))?$")


def point(name_part: str, plugin_label: str | None) -> str:
    """The point's own name: `heat.produced{purpose=dhw,by=total}` is "Heat produced,
    hot water (total)"."""
    match = _PATH.match(name_part)
    if match is None or match.group("name") not in POINTS:
        return plugin_label or name_part
    text = i18n._(POINTS[match.group("name")])
    qualifiers = dict(
        q.split("=", 1) for q in (match.group("qualifiers") or "").split(",") if "=" in q
    )
    purpose, by = qualifiers.get("purpose"), qualifiers.get("by")
    if purpose:
        text += ", " + i18n._(QUALIFIERS.get(purpose, purpose))
    if by:
        text += f" ({i18n._(QUALIFIERS.get(by, by))})"
    if match.group("n"):
        text += f" #{match.group('n')}"
    return text


def node(kind: str | None, path: str, label: str | None) -> str | None:
    """The node's name, or None for the device itself."""
    if kind is None or kind in ("unit", "site"):
        return None
    if label:
        return label
    if kind not in NODES:
        return kind
    number = re.search(r"(\d+)$", path)
    return i18n._(NODES[kind], n=number.group(1) if number else "")


def value(text: str) -> str:
    """A value as shown: translated where it is a standard one, and in sentence case
    whatever its source, so "idle" and the pump's own "Auto" look alike."""
    shown = i18n._(VALUES[text]) if text in VALUES else text
    return shown[:1].upper() + shown[1:]


QUANTITIES = {
    "temperature": mark("Temperature"),
    "humidity": mark("Humidity"),
    "absolute_humidity": mark("Absolute humidity"),
    "dew_point": mark("Dew point"),
    "carbon_dioxide": mark("CO\N{SUBSCRIPT TWO}"),
    "volatile_organic_compounds": mark("VOC (µg/m³)"),
    "volatile_organic_compounds_parts": mark("VOC (ppb)"),
    "pm25": mark("PM2.5"),
    "pm10": mark("PM10"),
    "atmospheric_pressure": mark("Air pressure"),
    "illuminance": mark("Light"),
    "irradiance": mark("Sunlight"),
    "power": mark("Power"),
    "energy": mark("Energy"),
    "volume_flow_rate": mark("Flow"),
    "water": mark("Water"),
    "heat_demand": mark("Heat demand"),
    "setpoint": mark("Setpoint"),
    "zone.open": mark("Heating"),
    "window.open": mark("Window open"),
    "window": mark("Window open"),
    "door": mark("Door open"),
    "opening": mark("Open"),
    "presence": mark("Someone home"),
    "occupancy": mark("Occupied"),
    "motion": mark("Motion"),
    "moisture": mark("Water leak"),
}

OWN_DEVICES = {
    "unknown": mark("not said yet"),
    "none": mark("nothing of its own"),
    "simple_thermostat": mark("a simple on/off thermostat"),
    "smart_thermostat": mark("a thermostat with an interface"),
    "radiator_valves": mark("radiator valves with an interface"),
    "zone_controller": mark("a zoning controller"),
}


def quantity(name: str) -> str:
    return i18n._(QUANTITIES[name]) if name in QUANTITIES else name


def own_device(kind: str) -> str:
    return i18n._(OWN_DEVICES[kind]) if kind in OWN_DEVICES else kind


QUALITY_COLORS = {
    "good": "green",
    "stale": "yellow",
    "transitional": "yellow",
    "assumed": "yellow",
    "out_of_range": "red",
    "not_connected": "grey",
    "no_flow": "grey",
    "unknown": "grey",
}
"""Green: use it. Yellow: a value, but not to be leaned on. Red: a value no device should
report. Grey: nothing usable."""

MARKED = (mark("uncertain"), mark("implausible"), mark("no usable value"))


def quality_color(quality: str) -> str:
    return QUALITY_COLORS.get(quality, "grey")
