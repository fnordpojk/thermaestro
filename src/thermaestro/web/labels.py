"""Names for people: the standard point names, their qualifiers and values, and the nodes
they sit on, in the request's language. A point outside the vocabulary shows the label
its plugin gives (a register's title), else its path."""

import re
from collections.abc import Mapping

from . import i18n
from .i18n import mark

POINTS = {
    "outdoor.temp": mark("Outdoor temperature"),
    "outdoor.temp.mean": mark("Outdoor temperature, mean"),
    "heating.stop_temp": mark("Heating stop"),
    "supply.temp": mark("Supply temperature"),
    "return.temp": mark("Return temperature"),
    "temp.top": mark("Top temperature"),
    "temp.mid": mark("Middle temperature"),
    "temp.charge": mark("Charging temperature"),
    "temp.start": mark("Start temperature"),
    "temp.stop": mark("Stop temperature"),
    "room.temp": mark("Room temperature"),
    "brine.in.temp": mark("Brine in"),
    "brine.delta_t": mark("Brine delta-T"),
    "heat.extracted.power": mark("Heat from the ground"),
    "heat.extracted": mark("Heat from the ground, total"),
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
    # forecast quantities
    "temperature.p10": mark("Temperature, 10th percentile"),
    "temperature.p90": mark("Temperature, 90th percentile"),
    "relative_humidity": mark("Humidity"),
    "cloud_cover": mark("Cloud cover"),
    "irradiance.global": mark("Sunlight"),
    "irradiance.direct_normal": mark("Direct sunlight"),
    "irradiance.diffuse": mark("Diffuse sunlight"),
    "wind_speed": mark("Wind"),
    "wind_gust": mark("Gusts"),
    "wind_direction": mark("Wind direction"),
    "precipitation": mark("Precipitation"),
    "pressure": mark("Air pressure"),
}

SHOWN_ONLY = mark("Shown only; Thermaestro doesn't plan with it.")
WITHOUT = {
    "temperature": mark("Without it, heat loss and the pump's efficiency can't be planned ahead."),
    "temperature.p10": mark(
        "Without it, the margin on firm goals comes from this house's own forecast errors only."
    ),
    "temperature.p90": mark(
        "Without it, the margin on firm goals comes from this house's own forecast errors only."
    ),
    "dew_point": mark(
        "Without it or humidity, there's no defrost estimate, and indoor humidity can't be"
        " predicted."
    ),
    "relative_humidity": mark(
        "Without it or the dew point, there's no defrost estimate, and indoor humidity can't"
        " be predicted."
    ),
    "irradiance.global": mark(
        "Without it, sun gains can't be planned ahead, only learned afterwards."
    ),
    "irradiance.direct_normal": mark(
        "Without it, sunlight on windows facing a direction is estimated from global sunlight."
    ),
    "irradiance.diffuse": mark(
        "Without it, sunlight on windows facing a direction is estimated from global sunlight."
    ),
    "cloud_cover": mark("Without it, sunlight can't be estimated where a provider gives none."),
    "wind_speed": mark("Without it, the extra heat loss in wind can't be planned ahead."),
}
"""What Thermaestro does without a forecast quantity, in plain words."""


def without(name: str) -> str:
    return i18n._(WITHOUT.get(name, SHOWN_ONLY))


KNOWN = {
    "documented": mark("from its documents"),
    "verified": mark("seen in its answers"),
    "observed": mark("seen in running"),
    "user": mark("as you set it"),
    "reported": mark("from a third party"),
    "refuted": mark("found to be false"),
    "unknown": mark("not known"),
}


def known(state: str) -> str:
    """How a fact is known, in words."""
    return i18n._(KNOWN[state]) if state in KNOWN else state


_DURATION = re.compile(r"^P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?)?$")
DURATION_PARTS = (mark("%(n)s d"), mark("%(n)s h"), mark("%(n)s min"))


def duration(code: str | None) -> str:
    """An ISO 8601 duration as people write it: `P2DT6H` is "2 d 6 h"."""
    match = _DURATION.match(code or "")
    if match is None:
        return code or ""
    parts = [
        i18n._(text, n=int(n)) for text, n in zip(DURATION_PARTS, match.groups(), strict=True) if n
    ]
    return " ".join(parts)


ROLES = {
    "energy.spot": mark("Spot price"),
    "energy.supplier": mark("Supplier"),
    "tax.energy": mark("Energy tax"),
    "grid.transfer": mark("Grid fee"),
    "grid.tou": mark("Grid fee by time of day"),
    "levy": mark("Levy"),
    "subsidy": mark("Subsidy"),
    "vat": mark("VAT"),
}
"""A price layer's role, in words, for the price chart's legend."""


def role(name: str) -> str:
    return i18n._(ROLES[name]) if name in ROLES else name


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


LEVERS = {
    "heating.offset": mark("Heating offset"),
    "mode": mark("Hot-water mode"),
    "block": mark("Hold its heating off"),
    "boost_once": mark("One extra charge"),
    "stop_temp": mark("Stop temperature"),
    "start_temp": mark("Start temperature"),
    "max_power": mark("Most power"),
    "alarm.reset": mark("Reset the alarm"),
}
"""The settings Thermaestro could change, by their standard name."""
MODES = {"off": mark("Off"), "shadow": mark("Shadow"), "control": mark("Control")}
EMITTERS = {
    "unknown": mark("Not said"),
    "radiators": mark("Radiators"),
    "floor": mark("Underfloor heating"),
    "radiators_and_floor": mark("Radiators and underfloor heating"),
    "fan_coils": mark("Fan coils"),
}
HOUSES = {
    "unknown": mark("Not said"),
    "poorly_insulated": mark("Poorly insulated"),
    "average": mark("Average"),
    "well_insulated": mark("Well insulated"),
    "low_energy": mark("Low-energy house"),
}
WATERS = {
    "unknown": mark("Not said"),
    "municipal": mark("Municipal water"),
    "well": mark("A private well"),
}
PAST_DEADLINE = {
    "keep_heating": mark("Keep heating until it is met, or the next one is due"),
    "stop": mark("Stop at the deadline"),
}


KINDS = {
    "comfort_band": mark("Comfort"),
    "hot_water_by": mark("Hot water by"),
    "hot_water_floor": mark("Lowest hot-water temperature"),
    "cost_stance": mark("Cost and comfort"),
    "addition_policy": mark("The addition"),
    "pool": mark("The pool"),
    "power_peak": mark("Power limit"),
    "quiet_hours": mark("Quiet hours"),
    "warmer": mark("Warmer, please"),
    "bath": mark("A bath"),
    "guests": mark("Guests"),
    "away": mark("Away"),
    "hands_off": mark("Hands off"),
    "fireplace": mark("A fireplace is on"),
    "boost_now": mark("Boost now"),
}
COOLER = mark("Cooler, please")


def intent_name(kind: str, parameters: Mapping[str, object]) -> str:
    """An intent's name: "warmer" with a negative offset is asked as cooler."""
    offset = parameters.get("offset")
    if kind == "warmer" and isinstance(offset, int | float) and offset < 0:
        return i18n._(COOLER)
    return i18n._(KINDS[kind]) if kind in KINDS else kind


RANKS = {
    "comfort_low": mark("Rooms not below their band"),
    "must_deadlines": mark("Hot water that must be ready"),
    "should_deadlines": mark("Hot water that should be ready"),
    "comfort_high": mark("Rooms not above their band"),
    "power_peak": mark("The power limit"),
}
POLICIES = {
    "pump": mark("As the pump does it"),
    "when_needed": mark("Only when the compressor can't keep up"),
    "not_when_expensive": mark("Not in dear hours"),
    "limit": mark("Within a power limit"),
}
STATES = {
    "received": mark("received"),
    "scheduled": mark("scheduled"),
    "active": mark("active"),
    "at_risk": mark("at risk"),
    "giving_way": mark("giving way"),
    "met": mark("met"),
    "missed": mark("missed"),
    "finished": mark("finished"),
    "rejected": mark("refused"),
}


OUTCOMES = {
    "verified": mark("done, and read back"),
    "not_kept": mark("accepted, but not kept"),
    "unverifiable": mark("sent; it can't be checked"),
    "awaiting_effect": mark("sent; its effect is awaited"),
    "timeout": mark("no answer in time"),
    "shadowed": mark("shadow: nothing was changed"),
    "dropped": mark("not delivered"),
    "device_refused": mark("the pump refused it"),
    "refused": mark("refused"),
    "unchanged": mark("already so"),
    "replaced": mark("a newer request replaced it"),
    "skipped": mark("not asked again yet"),
}


def lever(path: str) -> str:
    name = path.rpartition("/")[2]
    return i18n._(LEVERS[name]) if name in LEVERS else name


def choice(table: dict[str, str], key: str) -> str:
    return i18n._(table[key]) if key in table else key


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
