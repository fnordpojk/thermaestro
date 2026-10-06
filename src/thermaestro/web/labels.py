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
    return i18n._(VALUES[text]) if text in VALUES else text


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
