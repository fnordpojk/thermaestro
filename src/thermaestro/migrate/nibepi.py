"""NibePi's settings, read from its `config.json` into a draft of Thermaestro's: the pump
connection, the MQTT broker, external sensors, the location and price area, and secrets.

A pure function: nothing is written, nothing is reached, and nothing takes effect until
the household confirms the draft. Every key of the file is in the report, as carried over,
translated (to what), or left out (and why).

What tells NibePi's lines apart, tested in order: `update.version` 2.0.0 or a `log.hotwater`
or `system.docker` key (the consolidated fork); VV-AI keys or `price.vat`/`addition_ore`/
`apply_vat`/`enable_own_price` (pizzihelmet's VV-AI line, or the fork); `price.area`,
`price.time`, `price.min_spread`, `price.enable_freq`, `price.prio_*` or a `tcp` section
(1.2.1, or Åhsberg's, which the file can't tell apart); `update.release`, or `version` 1.1
without `tcp` and `update` (1.1). A NibePi 1.0 file (`plugins` or `defaultTopic`) has another
shape and is refused.

The gateway: NibePi's NibeGW client reads on port 10000 and writes on 10001, fixed in its
code. A broker on localhost was NibePi's own host's, which this one may not have.
"""

import json
import re
from dataclasses import dataclass, field
from typing import Any, Literal

from ..zones import ZONES

LOCALHOSTS = ("127.0.0.1", "localhost", "::1")
NIBEGW_READ, NIBEGW_WRITE = 10000, 10001
MQTT_SECRET = "mqtt.password"  # noqa: S105 - the name it is kept under
TIBBER_SECRET = "tibber.token"  # noqa: S105 - the name it is kept under
NONE = ("", "Ingen", None)
"""A feature's sensor choice meaning the pump's own room sensor."""
FEATURES = ("indoor", "price", "weather", "rmu")
"""The features that name a room sensor per climate system (`<feature>.sensor_sN`)."""


class NotNibePi(ValueError):
    """The file isn't a `config.json` this import reads."""


Outcome = Literal["carried", "translated", "left_out"]


@dataclass(frozen=True)
class Row:
    """A key of the file, and what became of it."""

    key: str
    outcome: Outcome
    note: str


@dataclass(frozen=True)
class Item:
    """One setting the draft would make. `kind` names what it is: `location`, `mqtt`,
    `discovery`, `pump`, `room`, `sensor`, `tibber`, `spot`, `layer` or `vat`."""

    kind: str
    id: str
    body: dict[str, Any]
    what: str
    optional: bool = False
    """Offered unticked: NibePi's figure may be stale, or never set."""


@dataclass
class Draft:
    line: str
    items: list[Item] = field(default_factory=list)
    secrets: dict[str, str] = field(default_factory=dict)
    """By the name each is kept under; never shown."""
    rows: list[Row] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    localhost_broker: bool = False
    timezone: str | None = None
    """From the price area; None where the area isn't known."""
    before: dict[int, float] = field(default_factory=dict)
    """For the review of the pump's settings: per register, its value before NibePi."""
    offsets: dict[int, float] = field(default_factory=dict)
    """For the review: per climate system, NibePi's own manual curve offset."""

    def item(self, key: str) -> Item | None:
        return next((i for i in self.items if f"{i.kind}:{i.id}" == key), None)


def _slug(name: str, taken: set[str]) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:56] or "x"
    id, n = base, 2
    while id in taken:
        id, n = f"{base}-{n}", n + 1
    taken.add(id)
    return id


def _flat(value: object, prefix: str = "") -> dict[str, object]:
    """Every leaf key, dotted; a list, an empty section or a Buffer object is one leaf."""
    if isinstance(value, dict) and value and value.get("type") != "Buffer":
        out: dict[str, object] = {}
        for k, v in value.items():
            out.update(_flat(v, f"{prefix}.{k}" if prefix else str(k)))
        return out
    return {prefix: value}


def _line(config: dict[str, Any], keys: set[str]) -> str:
    found = config.get("update")
    update: dict[str, Any] = found if isinstance(found, dict) else {}
    if update.get("version") == "2.0.0" or {"log.hotwater", "system.docker"} & keys:
        return "the consolidated fork"
    vv = any(k.startswith(("hotwater.vv_", "hotwater.enable_vv_")) for k in keys)
    pizzi = {"price.vat", "price.addition_ore", "price.apply_vat", "price.enable_own_price"}
    if vv or pizzi & keys:
        return "the VV-AI line or the consolidated fork"
    later = {"price.area", "price.time", "price.min_spread", "price.enable_freq"}
    if later & keys or any(k.startswith(("price.prio_", "tcp.")) for k in keys):
        return "1.2.1 or Åhsberg's"
    return "1.1"


def _text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _number(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip().replace(",", "."))
        except ValueError:
            return None
    return None


def _port(value: object, default: int) -> int:
    found = _number(value)
    return int(found) if found is not None and 0 < found < 65536 else default


LEFT_OUT: tuple[tuple[str, str], ...] = (
    ("price.", "a price control setting: Thermaestro plans by your intents and the price"),
    ("indoor.", "a room control setting: Thermaestro steers with its own loop"),
    ("weather.", "a forecast control setting: Thermaestro's loop handles weather itself"),
    ("hotwater.", "a hot-water setting: Thermaestro plans hot water by your intents"),
    ("fan.", "ventilation control isn't part of Thermaestro yet"),
    ("rmu.", "the RMU emulation has no counterpart"),
    ("plejd.", "Plejd isn't part of Thermaestro"),
    ("log.", "NibePi's logging"),
    ("data.", "NibePi's graph"),
    ("registers", "NibePi's register list: Thermaestro reads what it needs itself"),
    ("update.", "NibePi's version and updates"),
    ("version", "NibePi's version"),
    ("system.", "NibePi's own setting"),
    ("home.adjust_", "the curve offset NibePi added: offered as a baseline in the settings review"),
    ("home.hours_", "a forecast control setting: Thermaestro's loop handles weather itself"),
    ("home.size", "only shown by NibePi"),
    ("info.", "unused by NibePi"),
    ("serial.", "NibePi's serial or gateway setting"),
    ("tcp.", "NibePi's Modbus TCP setting"),
    ("connection.", "NibePi's connection setting"),
    ("mqtt.", "NibePi's MQTT setting"),
    ("home.", "NibePi's setting"),
)


def read(text: str, *, pump: str = "pump") -> Draft:
    """The draft a NibePi `config.json` makes. `pump`: the instance name for the pump.
    Raises NotNibePi for anything else, a NibePi 1.0 file included."""
    try:
        config = json.loads(text)
    except ValueError:
        raise NotNibePi("this isn't a JSON file") from None
    if not isinstance(config, dict):
        raise NotNibePi("this isn't a NibePi config.json")
    if "plugins" in config or "defaultTopic" in config:
        raise NotNibePi(
            "this is a NibePi 1.0 file, which this import doesn't read: set Thermaestro up"
            " by hand, or upgrade NibePi first"
        )
    if not any(k in config for k in ("connection", "serial", "mqtt", "home", "price")):
        raise NotNibePi("this isn't a NibePi config.json: no connection, MQTT or home section")
    flat = _flat(config)
    keys = set(flat)
    draft = Draft(line=_line(config, keys))
    done: dict[str, Row] = {}

    def row(key: str, outcome: Outcome, note: str) -> None:
        if key in flat:
            done[key] = Row(key, outcome, note)

    def section(name: str) -> dict[str, Any]:
        found = config.get(name)
        return found if isinstance(found, dict) else {}

    _pump(draft, section, row, pump)
    _mqtt(draft, section, row)
    rooms = _sensors(draft, section, row, pump)
    _location(draft, section, row)
    _price(draft, section, row)
    _for_review(draft, section, row)
    for key in ("plejd.pass", "plejd.cloud_name", "plejd.cloud_user", "plejd.cloud_pass"):
        row(key, "left_out", "a Plejd secret: Plejd isn't part of Thermaestro, so not imported")
    for key in sorted(keys - set(done)):
        why = next((w for prefix, w in LEFT_OUT if key.startswith(prefix)), "unused by NibePi")
        done[key] = Row(key, "left_out", why)
    draft.rows = [done[k] for k in sorted(done)]
    if rooms:
        draft.notes.append(
            "Each room sensor one of NibePi's features used becomes a room of its climate"
            " system; untick any you don't want, or move them between rooms later."
        )
    return draft


def _for_review(draft: Draft, section: Any, row: Any) -> None:
    """What the review of the pump's settings offers: the hot-water period NibePi's learning
    saved before holding it at 0, and NibePi's own curve offset per climate system (stored
    as the raw MQTT payload after a set over MQTT)."""
    period = _number(section("hotwater").get("vv_backup_hw_period"))
    if period is not None and period > 0:
        draft.before[47134] = period
        row(
            "hotwater.vv_backup_hw_period",
            "translated",
            f"the hot-water period before NibePi, {period:g} min: offered in the review of the"
            " pump's settings",
        )
    for key, value in section("home").items():
        match = re.fullmatch(r"adjust_s([1-4])", key)
        if match is None:
            continue
        offset = _number(_payload(value))
        if offset is None:
            continue
        draft.offsets[int(match[1])] = offset
        row(
            f"home.{key}",
            "translated",
            f"NibePi's own curve offset for climate system {match[1]}, {offset:g}: offered in"
            " the review of the pump's settings",
        )


def _payload(value: object) -> object:
    """A serialized Buffer's bytes as text; anything else as it is."""
    if isinstance(value, dict) and value.get("type") == "Buffer":
        data = value.get("data")
        if isinstance(data, list) and all(isinstance(b, int) and 0 <= b < 256 for b in data):
            return bytes(data).decode("utf-8", "replace")
        return None
    return value


def _pump(draft: Draft, section: Any, row: Any, pump: str) -> None:
    connection = section("connection")
    how = _text(connection.get("enable")) or "serial"
    port = _text(section("serial").get("port"))
    tcp = section("tcp")
    if how == "nibegw" and port:
        body = {"host": port, "read_port": NIBEGW_READ, "write_port": NIBEGW_WRITE}
        draft.items.append(
            Item("pump", pump, body, f"the pump through its NibeGW gateway at {port}")
        )
        row("connection.enable", "translated", "the pump through its NibeGW gateway")
        row(
            "serial.port",
            "translated",
            f"the gateway's address, {port}; NibePi's fixed ports 10000 (read) and 10001 (write)",
        )
    elif how == "tcp" and _text(tcp.get("host")):
        model = _text(tcp.get("pump"))
        body = {
            "host": _text(tcp.get("host")),
            "protocol": "modbus-tcp",
            "modbus_port": _port(tcp.get("port"), 502),
        }
        if model and model != "null":
            body["model"] = model
        else:
            draft.notes.append("An S-series pump over Modbus TCP needs its model: choose it.")
        draft.items.append(
            Item("pump", pump, body, f"the S-series pump over Modbus TCP at {body['host']}")
        )
        row("connection.enable", "translated", "the S-series pump over Modbus TCP")
        row("tcp.host", "carried", "the pump's address")
        row("tcp.port", "carried", "the Modbus TCP port")
        row("tcp.pump", "carried" if "model" in body else "left_out", "the pump's model")
        row("tcp.server", "left_out", "written by NibePi's editor, never read")
    else:
        note = (
            f"NibePi read the pump over a serial port ({port or 'not set'}). Thermaestro"
            " reaches it through a gateway: run thermaestro-gateway on that device, and"
            " enter its address under Setup → Pump."
        )
        draft.notes.append(note)
        row("connection.enable", "left_out", "a serial port: Thermaestro needs a gateway there")
        row("serial.port", "left_out", "a serial device: see the note on the gateway")
    for key in ("connection.series", "system.pump", "system.firmware"):
        row(key, "left_out", "a hint: Thermaestro identifies the pump itself")


def _mqtt(draft: Draft, section: Any, row: Any) -> None:
    mqtt = section("mqtt")
    host = _text(mqtt.get("host"))
    if not host:
        return
    body: dict[str, Any] = {
        "enabled": mqtt.get("enable") is not False,
        "host": host,
        "port": _port(mqtt.get("port"), 1883),
    }
    user, password = _text(mqtt.get("user")), mqtt.get("pass")
    if user:
        body["username"] = user
    if isinstance(password, str) and password:
        body["password"] = MQTT_SECRET
        draft.secrets[MQTT_SECRET] = password
        row("mqtt.pass", "carried", "into the secrets file")
    draft.items.append(Item("mqtt", "", body, f"the MQTT broker at {host}:{body['port']}"))
    for key in ("mqtt.enable", "mqtt.host", "mqtt.port", "mqtt.user"):
        row(key, "carried", "the MQTT broker")
    row(
        "mqtt.topic",
        "left_out",
        "Thermaestro publishes the house's state under its own topics, apart from NibePi's",
    )
    if "discovery" in mqtt:
        on = mqtt.get("discovery") is True
        draft.items.append(
            Item(
                "discovery",
                "",
                {"enabled": on},
                "Home Assistant discovery " + ("on" if on else "off"),
            )
        )
        row("mqtt.discovery", "carried", "Home Assistant discovery")
    if host in LOCALHOSTS:
        draft.localhost_broker = True
        draft.notes.append(
            "NibePi's broker ran on NibePi's own host. If this host has none, install one:"
            " Mosquitto from your system's packages, or a broker container next to"
            " Thermaestro's (then its address is the container's name, not localhost)."
        )


def _sensors(draft: Draft, section: Any, row: Any, pump: str) -> int:
    home = section("home")
    found = home.get("inside_sensors")
    sensors = [s for s in found if isinstance(s, dict)] if isinstance(found, list) else []
    timeout = _number(home.get("sensor_timeout"))
    freshness = timeout * 60 if timeout and timeout > 0 else None
    if "sensor_timeout" in home:
        row(
            "home.sensor_timeout",
            "translated",
            f"each sensor stale after {timeout:g} minutes"
            if freshness
            else "never stale in NibePi; Thermaestro learns each sensor's rhythm instead",
        )
    used: dict[str, list[int]] = {}
    for feature in FEATURES:
        part = section(feature)
        for n in range(1, 5):
            key = f"sensor_s{n}"
            name = part.get(key)
            if name in NONE or not isinstance(name, str):
                row(f"{feature}.{key}", "left_out", "the pump's own room sensor")
                continue
            systems = used.setdefault(name, [])
            if n not in systems:
                systems.append(n)
            row(f"{feature}.{key}", "translated", f"{name} is a room sensor of climate system {n}")
    taken_rooms: set[str] = set()
    taken_sensors: set[str] = set()
    rooms = 0
    for sensor in sensors:
        name, register, source = (
            _text(sensor.get("name")),
            _text(sensor.get("register")),
            sensor.get("source"),
        )
        if not name:
            continue
        if source != "mqtt" or not register:
            draft.notes.append(
                f"{name} is the pump's own register {register}: Thermaestro reads it through"
                " the pump."
            )
            continue
        body: dict[str, Any] = {"name": name, "source": "mqtt", "topic": register}
        if freshness:
            body["freshness_s"] = min(freshness, 12 * 3600)
        systems = used.get(name, [])
        if systems:
            room = _slug(name, taken_rooms)
            climate = f"{pump}:hp1/cs{systems[0]}"
            draft.items.append(
                Item(
                    "room",
                    room,
                    {"name": name, "climate_system": climate},
                    f"a room, {name}, of climate system {systems[0]}",
                )
            )
            body["room"] = room
            rooms += 1
            if len(systems) > 1:
                draft.notes.append(
                    f"{name} was used for climate systems {', '.join(map(str, systems))};"
                    f" its room is in system {systems[0]}."
                )
        id = _slug(name, taken_sensors)
        draft.items.append(Item("sensor", id, body, f"the MQTT sensor {name} on {register}"))
    if sensors:
        row("home.inside_sensors", "translated", f"{len(sensors)} sensors")
    return rooms


def _location(draft: Draft, section: Any, row: Any) -> None:
    home, price = section("home"), section("price")
    lat, lon = _number(home.get("lat")), _number(home.get("lon"))
    area = _text(price.get("area"))
    zone = ZONES.get(area)
    draft.timezone = zone.tz if zone is not None else None
    if lat is None or lon is None:
        return
    body = {"latitude": lat, "longitude": lon, "timezone": draft.timezone or "UTC"}
    draft.items.append(Item("location", "", body, f"the location, {lat:g}, {lon:g}"))
    row("home.lat", "carried", "the location")
    row("home.lon", "carried", "the location")
    if zone is None:
        draft.notes.append("The time zone couldn't be told from the price area: choose it.")


def _price(draft: Draft, section: Any, row: Any) -> None:
    price = section("price")
    source = price.get("source")
    area = _text(price.get("area"))
    token = price.get("token")
    zone = ZONES.get(area)
    if source == "tibber" and isinstance(token, str) and token:
        draft.secrets[TIBBER_SECRET] = token
        draft.items.append(
            Item("tibber", "tibber", {"token": TIBBER_SECRET, "home": None}, "prices from Tibber")
        )
        row("price.source", "translated", "prices from Tibber")
        row("price.token", "carried", "the Tibber token, into the secrets file")
        index = _number(price.get("tibber_home"))
        if index:
            draft.notes.append(
                f"NibePi took the prices of home {int(index) + 1} on the Tibber account;"
                " Thermaestro takes the first home with a contract until you choose."
            )
        row("price.tibber_home", "left_out", "Thermaestro finds the home with a contract")
    elif source == "nibe":
        row("price.source", "left_out", "the pump's Smart Price Adaption: keep it off")
        draft.notes.append(
            "NibePi took prices from the pump's Smart Price Adaption. Keep it off while"
            " Thermaestro plans: one owner per setting."
        )
    elif isinstance(token, str) and token:
        row(
            "price.token",
            "left_out",
            "an old NibePi cloud token or a leftover Tibber token: not imported",
        )
    if zone is not None:
        draft.items.append(Item("spot", area, {"zone": area}, f"the spot price for {area}"))
        row("price.area", "translated", f"the spot price for {area}, from its usual source")
        if source in ("priceai", "local_ai", ""):
            row("price.source", "translated", f"the spot price for {area}")
    elif area:
        row("price.area", "left_out", f"{area} isn't a bidding zone Thermaestro knows")
    unit = f"{zone.currency}/kWh" if zone is not None else None
    if unit is None:
        return
    rate = _number(price.get("vat"))
    if rate is not None and 0 < rate < 1:
        draft.items.append(
            Item("vat", "", {"rate": rate}, f"VAT at {rate * 100:g} %", optional=True)
        )
        row("price.vat", "translated", "VAT, offered")
    for key, role, what in (
        ("addition_ore", "energy.supplier", "the supplier's markup"),
        ("prio_tax", "tax.energy", "the energy tax"),
        ("prio_transfer", "grid.transfer", "the grid transfer fee"),
    ):
        ore = _number(price.get(key))
        if ore is None or ore <= 0:
            continue
        body = {
            "role": role,
            "source": "fixed",
            "value": round(ore / 100, 6),
            "unit": unit,
            "vat": "excl",
        }
        draft.items.append(
            Item("layer", role, body, f"{what}, {ore:g} öre/kWh as NibePi had it", optional=True)
        )
        row(f"price.{key}", "translated", f"{what}, offered as a price layer")
