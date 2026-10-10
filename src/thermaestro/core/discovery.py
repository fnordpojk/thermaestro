"""Home Assistant MQTT discovery: Thermaestro, and each device it reads, as devices in
Home Assistant.

- **Thermaestro** is a service device: the rooms' and the outdoor values, the electricity
  price now (today's and tomorrow's in its attributes), and how the plugins are doing.
- **Each device a plugin identifies** (a heat pump) is a device of its own, connected via
  Thermaestro, with its points. Its settings and diagnostic values are published too, but
  disabled until someone enables them in Home Assistant.
- **Rooms aren't devices:** Home Assistant has areas for them.
- **Sensors' own values** only when the household asks for them, since Home Assistant may
  have them already. Home Assistant's own entities are never sent back to it.

With control running, Thermaestro's device also has what the household asked for (the
intents in force, the next hot-water deadline, the last change made), each setting's mode,
and buttons for requests for a while: warmer and cooler per climate system, one extra
charge per tank, a fireplace.

Requests are read from one topic, as JSON in the API's household terms, and accepted only
with the rights the administrator gave the MQTT group: none at first. They are always for a
while. Otherwise the only topics read are Home Assistant's status, whose `online` (its
birth message) has everything sent again, and Thermaestro's own retained topics, read once
per connection to clear what is no longer published.

Topics, all retained but the requests and their answers:
- `<prefix>/device/thermaestro_<id>[_<device>]/config`: one discovery message per device;
- `<base>/<id>/status`: `online`, or `offline` (also the broker's last will);
- `<base>/<id>/state/<key>` and `<base>/<id>/attributes/<key>`;
- `<base>/<id>/request`: a request in; `<base>/<id>/request/result`: its answer.
"""

import asyncio
import contextlib
import hashlib
import json
import logging
import math
import re
import secrets
import time
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol
from zoneinfo import ZoneInfo

import aiomqtt

from .. import version
from ..cap.model import Envelope, Node, Point
from ..cap.vocabulary import STATES, bare
from ..store import Database, Discovery, Location, PriceLayer, Vat
from .gridrules import household
from .host import PluginHost, State
from .mqtt import MqttClient, Wanted
from .prices import assemble
from .sensors import SENSORS, SITE, SensorHub
from .series import Series
from .values import Key, Values

log = logging.getLogger(__name__)

SUPPORT_URL = "https://github.com/fnordpojk/thermaestro"
MIN_INTERVAL_S = 10.0
"""An entity's state goes out at most this often; a pump pushes some values twice a second."""
REBUILD_S = 5.0
PRICE_S = 60.0
CONTROL_S = 30.0
"""How often what the household asked for and the levers' modes are read again."""
SWEEP_S = 2.0
"""How long to collect the retained topics left at the broker."""
BIRTH_DELAY_S = (0.5, 3.0)
"""Home Assistant asks for a random pause before answering its birth message."""

NOT_DEVICES = frozenset({"homeassistant"})
"""Plugins whose values came from Home Assistant: never sent back to it."""
PLUGIN_STATES = tuple(str(s) for s in State)

HA_UNITS = {
    "degC": "\N{DEGREE SIGN}C",
    "g/m3": "g/m\N{SUPERSCRIPT THREE}",
    "ug/m3": "\N{GREEK SMALL LETTER MU}g/m\N{SUPERSCRIPT THREE}",
    "m3": "m\N{SUPERSCRIPT THREE}",
    "m3/h": "m\N{SUPERSCRIPT THREE}/h",
    "W/m2": "W/m\N{SUPERSCRIPT TWO}",
    "deg": "\N{DEGREE SIGN}",
    "degC.min": "\N{DEGREE SIGN}C\N{MIDDLE DOT}min",
}
"""Thermaestro's unit codes as Home Assistant writes them."""

BY_UNIT = {
    "degC": "temperature",
    "g/m3": "absolute_humidity",
    "hPa": "atmospheric_pressure",
    "lx": "illuminance",
    "W/m2": "irradiance",
    "W": "power",
    "kW": "power",
    "kWh": "energy",
    "L/min": "volume_flow_rate",
    "m3/h": "volume_flow_rate",
    "Hz": "frequency",
    "A": "current",
    "V": "voltage",
    "h": "duration",
}
"""Home Assistant's device class for a unit that has only one meaning here; each pair is
one Home Assistant accepts (its sensor component's table of units per device class)."""

BY_NAME = {
    ("%", "humidity"): "humidity",
    ("ppm", "carbon_dioxide"): "carbon_dioxide",
    ("ppb", "volatile_organic_compounds_parts"): "volatile_organic_compounds_parts",
    ("ug/m3", "volatile_organic_compounds"): "volatile_organic_compounds",
    ("ug/m3", "pm25"): "pm25",
    ("ug/m3", "pm10"): "pm10",
    ("L", "water"): "water",
}
"""A device class that depends on the quantity as well as the unit."""

TOTALS = frozenset({"energy", "water"})
MODES = ("off", "shadow", "control")

Asker = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]
"""Asks for an intent from a request in household terms, as the MQTT principal."""
ControlState = Callable[[], Awaitable[dict[str, Any]]]
"""What the household asked for and what the levers are at: `intents`, `deadline`,
`modes`, `last`."""


class Namer(Protocol):
    """Names for people, in the language asked for; the web UI's."""

    def point(self, instance: str, path: str, language: str) -> str: ...

    def node(self, instance: str, path: str) -> str | None:
        """The household's own name for a node, if it gave one."""
        ...

    def category(self, instance: str, path: str, given: str | None) -> str | None:
        """The household's category for a point where it chose one, else the plugin's."""
        ...

    def text(self, what: str, language: str, **values: object) -> str:
        """One of Thermaestro's own entity names: `price`, `plugin`, `attention`, and with
        control `warmer`, `cooler`, `boost`, `fireplace`, `intents`, `deadline`, `mode`,
        `last_change`."""
        ...

    def part(self, instance: str, path: str, language: str) -> str:
        """A node's name, the household's or the built-in one."""
        ...

    def lever(self, path: str, language: str) -> str:
        """A lever's name."""
        ...


@dataclass(frozen=True, slots=True)
class Topics:
    prefix: str
    base: str
    id: str

    @property
    def me(self) -> str:
        """The object id of Thermaestro's own device, and the start of every other one."""
        return f"thermaestro_{self.id}"

    @property
    def status(self) -> str:
        return f"{self.base}/{self.id}/status"

    @property
    def birth(self) -> str:
        return f"{self.prefix}/status"

    def device(self, object_id: str) -> str:
        return f"{self.prefix}/device/{object_id}/config"

    def state(self, key: str) -> str:
        return f"{self.base}/{self.id}/state/{key}"

    def attributes(self, key: str) -> str:
        return f"{self.base}/{self.id}/attributes/{key}"

    @property
    def request(self) -> str:
        return f"{self.base}/{self.id}/request"

    @property
    def result(self) -> str:
        return f"{self.base}/{self.id}/request/result"

    def ours(self, topic: str) -> bool:
        """A topic this installation publishes (or did)."""
        if topic.startswith(f"{self.base}/{self.id}/"):
            return True
        head = f"{self.prefix}/device/"
        if not topic.startswith(head) or not topic.endswith("/config"):
            return False
        object_id = topic[len(head) : -len("/config")]
        return object_id == self.me or object_id.startswith(f"{self.me}_")


def topics_of(setting: Discovery) -> Topics | None:
    return Topics(setting.prefix, setting.base, setting.id) if setting.id else None


def new_id() -> str:
    return secrets.token_hex(4)


def key(*parts: str) -> str:
    """A topic level and unique-id part for something: readable, and unique however its
    name was written (`a.b` and `a_b` stay apart)."""
    raw = ":".join(parts)
    readable = re.sub(r"[^a-z0-9]+", "_", raw.lower()).strip("_")[:48]
    return f"{readable}_{hashlib.sha256(raw.encode()).hexdigest()[:6]}"


@dataclass(frozen=True, slots=True)
class Device:
    object_id: str
    name: str
    manufacturer: str | None = None
    model: str | None = None
    sw_version: str | None = None
    serial: str | None = None
    via: str | None = None

    def info(self) -> dict[str, Any]:
        out: dict[str, Any] = {"identifiers": [self.object_id], "name": self.name}
        for field_name, value in (
            ("manufacturer", self.manufacturer),
            ("model", self.model),
            ("sw_version", self.sw_version),
            ("serial_number", self.serial),
            ("via_device", self.via),
        ):
            if value:
                out[field_name] = value
        return out


@dataclass(frozen=True, slots=True)
class Entity:
    key: str
    platform: str
    """`sensor` or `binary_sensor`."""
    name: str
    device: str
    """The object id of the device it belongs to."""
    device_class: str | None = None
    unit: str | None = None
    state_class: str | None = None
    diagnostic: bool = False
    enabled: bool = True
    options: tuple[str, ...] = ()
    """An enum's values; a value outside them is sent as unknown."""
    precision: int | None = None
    attributes: bool = False
    press: str | None = None
    """A button's request, sent to the request topic when pressed."""

    @property
    def numeric(self) -> bool:
        return self.platform == "sensor" and (self.unit is not None or self.state_class is not None)

    def config(self, topics: Topics) -> dict[str, Any]:
        if self.platform == "button":
            return {
                "platform": "button",
                "unique_id": f"{topics.me}_{self.key}",
                "name": self.name,
                "command_topic": topics.request,
                "payload_press": self.press,
            }
        out: dict[str, Any] = {
            "platform": self.platform,
            "unique_id": f"{topics.me}_{self.key}",
            "name": self.name,
            "state_topic": topics.state(self.key),
        }
        if self.device_class:
            out["device_class"] = self.device_class
        if self.unit:
            out["unit_of_measurement"] = self.unit
        if self.state_class:
            out["state_class"] = self.state_class
        if self.options:
            out["options"] = list(self.options)
        if self.precision is not None:
            out["suggested_display_precision"] = self.precision
        if self.diagnostic:
            out["entity_category"] = "diagnostic"
        if not self.enabled:
            out["enabled_by_default"] = False
        if self.attributes:
            out["json_attributes_topic"] = topics.attributes(self.key)
        return out

    def payload(self, value: object, digits: int | None = None) -> str:
        """A value as this entity's state; "None" is Home Assistant's word for unknown."""
        if value is None:
            return "None"
        if self.platform == "binary_sensor":
            return "ON" if value else "OFF"
        if self.options:
            return str(value) if str(value) in self.options else "None"
        if self.numeric:
            if isinstance(value, bool) or not isinstance(value, int | float):
                return "None"
            if digits is not None:
                return f"{float(value):.{digits}f}"
            return str(round(float(value), 3))
        return str(value)[:255]


@dataclass
class Model:
    """What is published: the devices, their entities, and where each entity's state
    comes from."""

    devices: dict[str, Device] = field(default_factory=dict)
    entities: dict[str, Entity] = field(default_factory=dict)
    points: dict[Key, str] = field(default_factory=dict)
    """A value's point, to the entity showing it."""
    digits: dict[str, int] = field(default_factory=dict)

    def discovery(self, topics: Topics, origin: dict[str, str]) -> dict[str, dict[str, Any]]:
        """Each device's discovery message, by its config topic."""
        out: dict[str, dict[str, Any]] = {}
        for object_id, device in sorted(self.devices.items()):
            components = {
                e.key: e.config(topics)
                for e in sorted(self.entities.values(), key=lambda e: e.key)
                if e.device == object_id
            }
            if not components:
                continue
            out[topics.device(object_id)] = {
                "device": device.info(),
                "origin": origin,
                "availability": [{"topic": topics.status}],
                "components": components,
            }
        return out


@dataclass(frozen=True, slots=True)
class Prices:
    unit: str
    state: str
    attributes: dict[str, Any]


def classify(name: str, unit: str | None, counter: bool) -> tuple[str | None, str | None]:
    """Home Assistant's device class and state class for a number, from the point's
    standard name (or quantity) and its unit."""
    quantity = bare(name).rpartition("/")[2]
    device_class = BY_UNIT.get(unit or "")
    for (u, q), found in BY_NAME.items():
        if unit == u and quantity.endswith(q):
            device_class = found
    if counter or device_class in TOTALS:
        return device_class, "total_increasing"
    if device_class == "duration":
        return device_class, None
    return device_class, "measurement"


def binary_class(name: str) -> str | None:
    quantity = bare(name).rpartition("/")[2]
    if quantity == "window.open":
        return "window"
    if quantity == "zone.open":
        return "opening"
    return quantity if quantity in STATES else None


def digits_of(point: Point | None) -> int | None:
    """The decimals a point's resolution gives it, if it gives one."""
    resolution = point.resolution.value if point is not None else None
    if resolution is None or resolution <= 0:
        return None
    return 0 if resolution >= 1 else min(3, max(0, round(-math.log10(resolution))))


class Publisher:
    """Publishes the model through the MQTT client while discovery is on, and clears what
    it published when it's switched off."""

    def __init__(
        self,
        db: Database,
        values: Values,
        host: PluginHost | None,
        sensors: SensorHub | None,
        series: Series | None,
        client: MqttClient,
        *,
        clock: Callable[[], float] = time.time,
        min_interval_s: float = MIN_INTERVAL_S,
        rebuild_s: float = REBUILD_S,
        sweep_s: float = SWEEP_S,
        birth_delay_s: tuple[float, float] = BIRTH_DELAY_S,
    ) -> None:
        self._db = db
        self._values = values
        self._host = host
        self._sensors = sensors
        self._series = series
        self._client = client
        self._clock = clock
        self._min_interval = min_interval_s
        self._rebuild = rebuild_s
        self._sweep_s = sweep_s
        self._birth_delay = birth_delay_s
        self.namer: Namer | None = None
        self.model = Model()
        self.state = "off"
        """off, sweeping or publishing."""
        self._setting: Discovery | None = None
        self._topics: Topics | None = None
        self._old: list[Topics] = []
        self._swept = False
        self._collecting: dict[str, bytes] | None = None
        self._birth = asyncio.Event()
        self._last: dict[str, str] = {}
        """What each state and attribute topic last carried."""
        self._sent_at: dict[str, float] = {}
        self._pending: dict[str, str] = {}
        self._priced: Prices | None = None
        self._priced_at = float("-inf")
        self._platforms: dict[Key, Entity] = {}
        """Entities already made for points, kept while a point has no value."""
        self._waiting: set[Key] = set()
        """Points left out until their first value says what kind of entity they are."""
        self._dirty = False
        self._signed: tuple[object, ...] = ()
        self.asker: Asker | None = None
        self.control: ControlState | None = None
        self._requests: asyncio.Queue[bytes] = asyncio.Queue(maxsize=20)
        self._modes: dict[str, str] = {}
        """Each lever's mode entity, to the lever's reference."""
        self._controlled: tuple[float, dict[str, Any]] | None = None
        values.listeners.append(self._heard)
        client.outward = self

    def start(self, namer: Namer) -> None:
        """Begin, once names can be given."""
        self.namer = namer
        self._client.reload()

    def changed(self, before: Discovery | None, after: Discovery) -> None:
        """The setting changed: start over, and clear what was published under topics no
        longer used."""
        old = topics_of(before) if before is not None else None
        if old is not None and old != topics_of(after):
            self._old.append(old)
        self._swept = False
        self._client.reload()

    # --- the client's side ---------------------------------------------------------------

    async def wanted(self) -> Wanted:
        if self.namer is None:
            return None
        setting = await self._db.get(Discovery)
        self._setting = setting
        self._topics = topics_of(setting) if setting is not None else None
        if setting is not None and setting.enabled and self._topics is not None:
            return "publish"
        if (self._topics is not None or self._old) and not self._swept:
            return "sweep"
        return None

    def will(self) -> aiomqtt.Will | None:
        if self._topics is None:
            return None
        return aiomqtt.Will(self._topics.status, b"offline", qos=1, retain=True)

    def handles(self, topic: str) -> bool:
        if self._topics is not None and topic in (self._topics.birth, self._topics.request):
            return True
        return self._collecting is not None and any(t.ours(topic) for t in self._spaces())

    def receive(self, topic: str, payload: bytes, retained: bool) -> None:
        if self._collecting is not None and any(t.ours(topic) for t in self._spaces()):
            if retained and payload:
                self._collecting[topic] = payload
            return
        if self._topics is not None and topic == self._topics.birth and payload == b"online":
            self._birth.set()
        elif self._topics is not None and topic == self._topics.request and not retained:
            # A retained request would be asked again at every connection: only live ones.
            with contextlib.suppress(asyncio.QueueFull):
                self._requests.put_nowait(payload)

    async def session(self, client: aiomqtt.Client, wanted: Wanted) -> None:
        if wanted == "sweep":
            self.state = "sweeping"
            await self._sweep(client, keep=set())
            self._old.clear()
            self._swept = True
            self.state = "off"
            log.info("Home Assistant discovery: cleared what was published")
            return
        topics = self._topics
        assert topics is not None  # noqa: S101 - wanted() said publish
        self.state = "publishing"
        self._last, self._sent_at, self._pending = {}, {}, {}
        try:
            self.model = await self._build()
            await self._sweep(client, keep=self._kept(topics))
            self._old.clear()
            await client.subscribe(topics.birth)
            if self.asker is not None:
                await client.subscribe(topics.request)
            await self._publish_all(client)
            log.info(
                "Home Assistant discovery: %d devices, %d entities",
                len(self.model.devices),
                len(self.model.entities),
            )
            await self._loop(client)
        finally:
            self.state = "off"
            with contextlib.suppress(aiomqtt.MqttError):
                await client.publish(topics.status, b"offline", qos=1, retain=True)

    # --- publishing ----------------------------------------------------------------------

    async def _loop(self, client: aiomqtt.Client) -> None:
        rebuilt = prices = self._clock()
        while True:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._birth.wait(), 1.0)
            if self._birth.is_set():
                self._birth.clear()
                low, high = self._birth_delay
                await asyncio.sleep(low + (high - low) * secrets.randbelow(1000) / 1000)
                self._last = {}
                await self._publish_all(client)
                continue
            while not self._requests.empty():
                await self._answer(client, self._requests.get_nowait())
            now = self._clock()
            if now - rebuilt >= self._rebuild:
                rebuilt = now
                if now - prices >= PRICE_S:
                    prices = now
                    await self._prices()
                signature = self._signature()
                if self._dirty or not _same(signature, self._signed):
                    self._dirty, self._signed = False, signature
                    await self._publish_changed(client, await self._build())
                await self._publish_derived(client)
            await self._flush(client)

    async def _answer(self, client: aiomqtt.Client, payload: bytes) -> None:
        """Ask for what a request says, and publish the answer."""
        topics, asker = self._topics, self.asker
        if topics is None or asker is None:
            return
        try:
            body = json.loads(payload)
            if not isinstance(body, dict):
                raise ValueError("a request is a JSON object")
            answer = await asker(body)
        except ValueError as e:
            answer = {"accepted": False, "messages": [f"not a request: {e}"]}
        except Exception:
            log.exception("an MQTT request failed")
            answer = {"accepted": False, "messages": ["it failed; see Thermaestro's log"]}
        await client.publish(topics.result, json.dumps(answer, ensure_ascii=False), qos=1)
        self._controlled = None  # shown at the next round, not in half a minute

    def _signature(self) -> tuple[object, ...]:
        """What the model is made from, so it's made again only when that changed: the
        plugins and what they described, which points have values, the household's
        names, rooms, sensors and categories, and whether there is a price. The objects
        themselves are kept, so their ids can't be reused while compared."""
        host = self._host.instances.values() if self._host is not None else ()
        hub = self._sensors
        return (
            *((i.id, i.setting, i.described) for i in host),
            len(self._values.latest),
            *((hub.names, hub.display, hub.rooms, hub.sensors) if hub is not None else ()),
            self._priced.unit if self._priced is not None else None,
        )

    async def _publish_all(self, client: aiomqtt.Client) -> None:
        topics = self._topics
        assert topics is not None  # noqa: S101
        for topic, message in self.model.discovery(topics, self._origin()).items():
            await client.publish(topic, json.dumps(message), qos=1, retain=True)
        await client.publish(topics.status, b"online", qos=1, retain=True)
        for entity_key, payload in self._states().items():
            await self._send(client, topics.state(entity_key), payload)
        await self._publish_derived(client, force=True)

    async def _publish_changed(self, client: aiomqtt.Client, model: Model) -> None:
        """Send the discovery messages of the devices that changed. A removed entity is
        first sent with its platform only, which has Home Assistant delete it, and then
        left out; a removed device gets an empty message."""
        topics = self._topics
        assert topics is not None  # noqa: S101
        origin = self._origin()
        before = self.model.discovery(topics, origin)
        after = model.discovery(topics, origin)
        old_model, self.model = self.model, model
        for topic, message in after.items():
            if before.get(topic) == message:
                continue
            gone = (
                set(before[topic]["components"]) - set(message["components"])
                if (topic in before)
                else set()
            )
            if gone:
                removal = {
                    **message,
                    "components": {
                        **message["components"],
                        **{k: {"platform": old_model.entities[k].platform} for k in gone},
                    },
                }
                await client.publish(topic, json.dumps(removal), qos=1, retain=True)
            await client.publish(topic, json.dumps(message), qos=1, retain=True)
        for topic in set(before) - set(after):
            await client.publish(topic, b"", qos=1, retain=True)
        for entity_key in set(old_model.entities) - set(model.entities):
            for t in (topics.state(entity_key), topics.attributes(entity_key)):
                if t in self._last:
                    await client.publish(t, b"", qos=1, retain=True)
                    self._last.pop(t, None)
        for entity_key, payload in self._states().items():
            if entity_key not in old_model.entities:
                await self._send(client, topics.state(entity_key), payload)
        await self._publish_derived(client)

    def _states(self) -> dict[str, str]:
        out = {}
        for point_key, entity_key in self.model.points.items():
            envelope = self._values.latest.get(point_key)
            out[entity_key] = self._payload(entity_key, envelope)
        return out

    def _payload(self, entity_key: str, envelope: Envelope | None) -> str:
        entity = self.model.entities[entity_key]
        good = envelope is not None and envelope.quality == "good"
        value = envelope.value if good and envelope is not None else None
        return entity.payload(value, self.model.digits.get(entity_key))

    def _heard(self, instance: str, envelope: Envelope) -> None:
        if self.state != "publishing" or self._topics is None:
            return
        point_key = Key(instance, envelope.point)
        entity_key = self.model.points.get(point_key)
        if entity_key is None:
            if point_key in self._waiting and envelope.value is not None:
                self._dirty = True  # now it can be published
            return
        topic = self._topics.state(entity_key)
        payload = self._payload(entity_key, envelope)
        if self._last.get(topic) != payload:
            self._pending[topic] = payload
        else:
            self._pending.pop(topic, None)

    async def _flush(self, client: aiomqtt.Client) -> None:
        now = self._clock()
        for topic, payload in list(self._pending.items()):
            if now - self._sent_at.get(topic, 0.0) >= self._min_interval:
                del self._pending[topic]
                await self._send(client, topic, payload)

    async def _send(
        self, client: aiomqtt.Client, topic: str, payload: str, *, force: bool = False
    ) -> None:
        if not force and self._last.get(topic) == payload:
            return
        await client.publish(topic, payload.encode(), qos=0, retain=True)
        self._last[topic] = payload
        self._sent_at[topic] = self._clock()

    async def _publish_derived(self, client: aiomqtt.Client, *, force: bool = False) -> None:
        """The entities that aren't points: the price now, and the plugins' states."""
        topics = self._topics
        assert topics is not None  # noqa: S101
        for entity_key, (state, attributes) in (await self._derived()).items():
            if entity_key not in self.model.entities:
                continue
            await self._send(client, topics.state(entity_key), state, force=force)
            if attributes is not None:
                text = json.dumps(attributes, ensure_ascii=False)
                await self._send(client, topics.attributes(entity_key), text, force=force)

    # --- sweeping ------------------------------------------------------------------------

    def _spaces(self) -> list[Topics]:
        return [*self._old, *([self._topics] if self._topics is not None else [])]

    def _kept(self, topics: Topics) -> set[str]:
        kept = set(self.model.discovery(topics, self._origin())) | {topics.status}
        for entity in self.model.entities.values():
            kept.add(topics.state(entity.key))
            if entity.attributes:
                kept.add(topics.attributes(entity.key))
        return kept

    async def _sweep(self, client: aiomqtt.Client, keep: set[str]) -> None:
        """Clear the retained topics this installation left at the broker that aren't
        published any more."""
        self._collecting = {}
        patterns = []
        for t in self._spaces():
            patterns += [f"{t.prefix}/device/+/config", f"{t.base}/{t.id}/#"]
        patterns = sorted(set(patterns))
        try:
            for pattern in patterns:
                await client.subscribe(pattern)
            await asyncio.sleep(self._sweep_s)
            for pattern in patterns:
                await client.unsubscribe(pattern)
            found = self._collecting
        finally:
            self._collecting = None
        for topic in sorted(set(found) - keep):
            await client.publish(topic, b"", qos=1, retain=True)

    # --- the model -----------------------------------------------------------------------

    def _origin(self) -> dict[str, str]:
        return {"name": "Thermaestro", "sw_version": version(), "support_url": SUPPORT_URL}

    async def _build(self) -> Model:
        setting, topics, namer = self._setting, self._topics, self.namer
        if setting is None or topics is None or namer is None:
            return Model()
        language = setting.language
        model = Model()
        me = Device(topics.me, "Thermaestro", "Thermaestro", "Thermaestro", version())
        model.devices[me.object_id] = me
        for point_key in sorted(self._values.latest, key=lambda k: (k.instance, k.point)):
            if point_key.instance == SITE or (point_key.instance == SENSORS and setting.sensors):
                name = namer.point(point_key.instance, point_key.point, language)
                self._add_point(model, point_key, None, name, me.object_id, None)
        if self._host is not None:
            for id, instance in sorted(self._host.instances.items()):
                self._add_status(model, id, namer.text("plugin", language, name=id), me)
                described = instance.described
                if instance.setting.plugin in NOT_DEVICES or described is None:
                    continue
                devices = self._devices(id, described.nodes, me.object_id)
                for device in devices.values():
                    model.devices[device.object_id] = device
                for point in described.points:
                    device_id = _device_of(point.path, devices)
                    if device_id is None:
                        continue
                    category = namer.category(id, point.path, point.category)
                    name = namer.point(id, point.path, language)
                    self._add_point(model, Key(id, point.path), point, name, device_id, category)
            if self._host.instances:
                attention = key("thermaestro", "attention")
                model.entities[attention] = Entity(
                    attention,
                    "binary_sensor",
                    namer.text("attention", language),
                    me.object_id,
                    device_class="problem",
                    diagnostic=True,
                )
        if self.control is not None:
            self._add_control(model, language, me)
        prices = await self._prices(fresh=False)
        if prices is not None:
            price = key("thermaestro", "price")
            model.entities[price] = Entity(
                price,
                "sensor",
                namer.text("price", language),
                me.object_id,
                unit=prices.unit,
                state_class="measurement",
                attributes=True,
            )
        return model

    def _devices(self, instance: str, nodes: tuple[Node, ...], via: str) -> dict[str, Device]:
        """The nodes a plugin identifies, each a device, by node path."""
        out = {}
        namer = self.namer
        assert namer is not None  # noqa: S101
        for node in nodes:
            identity = node.identity
            if identity is None:
                continue
            object_id = f"{via}_{key(instance, node.path)}"
            shown = node.label or identity.model  # the pump's own product name, if it gave one
            if not shown.lower().startswith(identity.vendor.lower()):
                shown = f"{identity.vendor} {shown}"
            out[node.path] = Device(
                object_id,
                namer.node(instance, node.path) or shown,
                identity.vendor,
                identity.model,
                identity.firmware,
                identity.serial,
                via,
            )
        return out

    def _add_point(
        self,
        model: Model,
        point_key: Key,
        point: Point | None,
        name: str,
        device: str,
        category: str | None,
    ) -> None:
        envelope = self._values.latest.get(point_key)
        entity_key = key(point_key.instance, point_key.point)
        made = self._entity(entity_key, point_key, point, envelope, name, device, category)
        if made is None:
            self._waiting.add(point_key)
            return
        self._waiting.discard(point_key)
        self._platforms[point_key] = made
        model.entities[entity_key] = made
        model.points[point_key] = entity_key
        found = digits_of(point)
        if found is not None and made.numeric:
            model.digits[entity_key] = found

    def _entity(
        self,
        entity_key: str,
        point_key: Key,
        point: Point | None,
        envelope: Envelope | None,
        name: str,
        device: str,
        category: str | None,
    ) -> Entity | None:
        """The entity for a point: a sensor, an on/off one, or one of an enum's values.
        Which one is known from its first value, so a point that has never had one isn't
        published yet."""
        hidden = category is not None
        path = point_key.point
        if point is not None and point.enum.value:
            return Entity(
                entity_key,
                "sensor",
                name,
                device,
                device_class="enum",
                options=tuple(sorted(point.enum.value)),
                diagnostic=hidden,
                enabled=not hidden,
            )
        value = envelope.value if envelope is not None else None
        if value is None:
            earlier = self._platforms.get(point_key)
            if earlier is None:
                return None
            return Entity(
                entity_key,
                earlier.platform,
                name,
                device,
                earlier.device_class,
                earlier.unit,
                earlier.state_class,
                hidden,
                not hidden,
                earlier.options,
                earlier.precision,
            )
        if isinstance(value, bool):
            return Entity(
                entity_key,
                "binary_sensor",
                name,
                device,
                device_class=binary_class(path),
                diagnostic=hidden,
                enabled=not hidden,
            )
        if isinstance(value, int | float):
            unit = point.unit if point is not None else envelope.unit if envelope else None
            counter = point is not None and point.wraps_at is not None
            device_class, state_class = classify(path, unit, counter)
            return Entity(
                entity_key,
                "sensor",
                name,
                device,
                device_class=device_class,
                unit=HA_UNITS.get(unit, unit) if unit else None,
                state_class=state_class,
                diagnostic=hidden,
                enabled=not hidden,
                precision=digits_of(point),
            )
        return Entity(entity_key, "sensor", name, device, diagnostic=hidden, enabled=not hidden)

    def _add_control(self, model: Model, language: str, me: Device) -> None:
        """What the household asked for, the levers' modes, and buttons for requests."""
        namer = self.namer
        assert namer is not None  # noqa: S101
        for what in ("intents", "last_change"):
            entity_key = key("thermaestro", what)
            model.entities[entity_key] = Entity(
                entity_key, "sensor", namer.text(what, language), me.object_id, attributes=True
            )
        deadline = key("thermaestro", "deadline")
        model.entities[deadline] = Entity(
            deadline,
            "sensor",
            namer.text("deadline", language),
            me.object_id,
            device_class="timestamp",
            attributes=True,
        )
        fireplace = key("request", "fireplace")
        model.entities[fireplace] = Entity(
            fireplace,
            "button",
            namer.text("fireplace", language),
            me.object_id,
            press=json.dumps({"kind": "fireplace"}),
        )
        for id, instance in sorted((self._host.instances if self._host else {}).items()):
            described = instance.described
            if described is None or instance.setting.plugin in NOT_DEVICES:
                continue
            devices = self._devices(id, described.nodes, me.object_id)
            for node in described.nodes:
                scope = f"{id}:{node.path}"
                part = namer.part(id, node.path, language)
                device = _device_of(node.path + "/x", devices) or me.object_id
                if node.kind == "climate_system":
                    for what, offset in (("warmer", 1), ("cooler", -1)):
                        entity_key = key("request", what, scope)
                        press = {"kind": "warmer", "scope": scope, "offset": offset}
                        model.entities[entity_key] = Entity(
                            entity_key,
                            "button",
                            namer.text(what, language, part=part),
                            device,
                            press=json.dumps(press),
                        )
                elif node.kind == "dhw_tank":
                    entity_key = key("request", "boost", scope)
                    model.entities[entity_key] = Entity(
                        entity_key,
                        "button",
                        namer.text("boost", language, part=part),
                        device,
                        press=json.dumps({"kind": "boost_now", "scope": scope}),
                    )
            for lever in described.levers:
                if lever.unavailable or lever.path.endswith("alarm.reset"):
                    continue
                entity_key = key("mode", id, lever.path)
                self._modes[entity_key] = f"{id}:{lever.path}"
                model.entities[entity_key] = Entity(
                    entity_key,
                    "sensor",
                    namer.text("mode", language, lever=namer.lever(lever.path, language)),
                    _device_of(lever.path, devices) or me.object_id,
                    device_class="enum",
                    options=MODES,
                    diagnostic=True,
                )

    def _add_status(self, model: Model, instance: str, name: str, me: Device) -> None:
        entity_key = key("status", instance)
        model.entities[entity_key] = Entity(
            entity_key,
            "sensor",
            name,
            me.object_id,
            device_class="enum",
            options=PLUGIN_STATES,
            diagnostic=True,
        )

    async def _derived(self) -> dict[str, tuple[str, dict[str, Any] | None]]:
        out: dict[str, tuple[str, dict[str, Any] | None]] = {}
        if self._host is not None:
            attention = False
            for id, instance in self._host.instances.items():
                out[key("status", id)] = (str(instance.state), None)
                health = instance.health
                attention = attention or instance.state != State.UP or bool(instance.active)
                if health is not None:
                    attention = attention or bool(
                        health.state != "up" or health.needs_user_action or health.stale
                    )
            out[key("thermaestro", "attention")] = ("ON" if attention else "OFF", None)
        price = key("thermaestro", "price")
        if price in self.model.entities:
            prices = await self._prices(fresh=False)
            if prices is not None:
                out[price] = (prices.state, prices.attributes)
        if self.control is not None and key("thermaestro", "intents") in self.model.entities:
            if self._controlled is None or self._clock() - self._controlled[0] >= CONTROL_S:
                self._controlled = (self._clock(), await self.control())
            state = self._controlled[1]
            out[key("thermaestro", "intents")] = (
                str(len(state["intents"])),
                {"intents": state["intents"]},
            )
            due = state["deadline"]
            out[key("thermaestro", "deadline")] = (due["t"], due) if due else ("None", {})
            last = state["last"]
            out[key("thermaestro", "last_change")] = (
                f"{last['lever']}: {last['outcome']}"[:255] if last else "None",
                last or {},
            )
            for entity_key, ref in self._modes.items():
                out[entity_key] = (state["modes"].get(ref, "off"), None)
        return out

    async def _prices(self, *, fresh: bool = True) -> Prices | None:
        """The price now, today's and tomorrow's, and whom to credit for them; worked out
        again at most once a minute unless `fresh`."""
        if not fresh and self._clock() - self._priced_at < PRICE_S:
            return self._priced
        self._priced = await self._work_out_prices()
        self._priced_at = self._clock()
        return self._priced

    async def _work_out_prices(self) -> Prices | None:
        if self._series is None:
            return None
        location = await self._db.get(Location)
        layers = await self._db.all(PriceLayer)
        if location is None or not layers:
            return None
        zone = ZoneInfo(location.timezone)
        vat = await self._db.get(Vat)
        today = datetime.fromtimestamp(self._clock(), zone).date()
        rules, holiday = await household(self._db, zone)
        stacks = [
            await assemble(layers, vat, self._series, day, zone, rules, holiday)
            for day in (today, today + timedelta(days=1))
        ]
        if not stacks[0].unit or not stacks[0].slots:
            return None
        now = datetime.fromtimestamp(self._clock(), UTC)
        current = next((s.total for s in stacks[0].slots if s.start <= now < s.end), None)
        attributes: dict[str, Any] = {
            day: [
                {"start": s.start.isoformat(), "price": round(s.total, 4)}
                for s in stack.slots
                if s.total is not None
            ]
            for day, stack in zip(("today", "tomorrow"), stacks, strict=True)
        }
        credit = self._attribution(layers.values())
        if credit:
            attributes["attribution"] = credit
        state = "None" if current is None else str(round(current, 4))
        return Prices(stacks[0].unit, state, attributes)

    def _attribution(self, layers: Iterable[PriceLayer]) -> str:
        """What the price sources' terms ask to be credited with wherever their prices
        are shown: ENTSO-E's, for one."""
        if self._host is None:
            return ""
        instances: list[str] = []
        for layer in layers:
            if layer.source == "series" and layer.plugin:
                instances.append(layer.plugin)
            instances += [ref.partition(":")[0] for ref in layer.fallbacks]
        texts: list[str] = []
        for id in instances:
            instance = self._host.instances.get(id)
            described = instance.described if instance is not None else None
            provider = described.provider if described is not None else None
            text = provider.terms.attribution.value if provider is not None else None
            if text and text not in texts:
                texts.append(text)
        return "; ".join(texts)


def _same(a: tuple[object, ...], b: tuple[object, ...]) -> bool:
    return len(a) == len(b) and all(x is y or x == y for x, y in zip(a, b, strict=True))


def _device_of(path: str, devices: dict[str, Device]) -> str | None:
    """The device a point belongs to: the deepest identified node above it."""
    best = None
    for node_path, device in devices.items():
        if path.startswith(f"{node_path}/") and (best is None or len(node_path) > len(best[0])):
            best = (node_path, device.object_id)
    return best[1] if best else None
