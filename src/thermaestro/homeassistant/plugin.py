"""The Home Assistant plugin: the chosen entities' states, read through HA's WebSocket API.

It logs in with a long-lived access token from the secrets file and subscribes to the
chosen entities only (`subscribe_entities`), so HA sends their states once and then each
change. Nothing is written to Home Assistant.

The device tree: a node `ha`; a sensor or binary sensor is a point under the plugin's
own names, `ha/x.homeassistant.<entity>`, since what it measures is said by the sensor
the user makes of it; a climate entity (a thermostat or a radiator valve) is a room node
`ha/<entity>` with the standard points a room has: `temperature`, `setpoint`,
`zone.open` (heating now), and `heat_demand` and `window.open` where the device reports
them.

Values come in Thermaestro's stored units (°F becomes °C, mbar hPa, and so on). HA's
`unavailable` and `unknown` become quality `unknown`; the time observed is when HA last
heard from the device.
"""

import asyncio
import contextlib
import itertools
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import aiohttp

from ..cap import Message, Send
from ..cap.messages import (
    Act,
    Describe,
    Described,
    Error,
    Fate,
    Health,
    Read,
    Subscribe,
    Update,
    Values,
)
from ..cap.model import Delivery, Envelope, Knowledge, Node, Point, Presence
from ..cap.vocabulary import QUANTITIES, STATES
from ..core.plugins import PluginContext
from ..store import HomeAssistant, SecretStore

log = logging.getLogger(__name__)

ROOT = "ha"
OWN = "x.homeassistant."
FRESHNESS_S = 3600.0
READ_AFTER_WAIT_S = 2.0
"""How long a read with `after` waits for Home Assistant to send something newer."""

UNITS: dict[str, tuple[str, Callable[[float], float]]] = {
    "\N{DEGREE SIGN}C": ("degC", lambda v: v),
    "\N{DEGREE SIGN}F": ("degC", lambda v: (v - 32) * 5 / 9),
    "K": ("degC", lambda v: v - 273.15),
    "%": ("%", lambda v: v),
    "ppm": ("ppm", lambda v: v),
    "ppb": ("ppb", lambda v: v),
    "hPa": ("hPa", lambda v: v),
    "mbar": ("hPa", lambda v: v),
    "Pa": ("hPa", lambda v: v / 100),
    "kPa": ("hPa", lambda v: v * 10),
    "inHg": ("hPa", lambda v: v * 33.8639),
    "W": ("W", lambda v: v),
    "kW": ("W", lambda v: v * 1000),
    "Wh": ("kWh", lambda v: v / 1000),
    "kWh": ("kWh", lambda v: v),
    "MWh": ("kWh", lambda v: v * 1000),
    "lx": ("lx", lambda v: v),
    "W/m\N{SUPERSCRIPT TWO}": ("W/m2", lambda v: v),
    "\N{MICRO SIGN}g/m\N{SUPERSCRIPT THREE}": ("ug/m3", lambda v: v),
    "\N{GREEK SMALL LETTER MU}g/m\N{SUPERSCRIPT THREE}": ("ug/m3", lambda v: v),
    "g/m\N{SUPERSCRIPT THREE}": ("g/m3", lambda v: v),
    "L/min": ("L/min", lambda v: v),
    "m\N{SUPERSCRIPT THREE}/h": ("m3/h", lambda v: v),
    "L": ("L", lambda v: v),
    "m\N{SUPERSCRIPT THREE}": ("L", lambda v: v * 1000),
}
"""Home Assistant's units to the ones Thermaestro stores."""

DEMAND_ATTRIBUTES = ("pi_heating_demand", "heating_demand", "valve_position")
WINDOW_ATTRIBUTES = ("window_open", "window_detection")
MISSING_STATES = ("unavailable", "unknown", "")


class AuthFailed(Exception):
    pass


@dataclass
class EntityInfo:
    """An entity that can be read, for the setup page."""

    entity_id: str
    name: str
    domain: str
    device_class: str | None
    unit: str | None
    area: str | None
    quantity: str | None
    """What it would be read as: a vocabulary quantity, or None for a climate entity."""
    points: tuple[tuple[str, str], ...] = ()
    """The points it gives, with their quantities: (path, quantity)."""


# --- the connection ---------------------------------------------------------------------


class Connection:
    """One logged-in WebSocket session to Home Assistant."""

    def __init__(self, ws: aiohttp.ClientWebSocketResponse, version: str | None) -> None:
        self.ws = ws
        self.version = version
        self._ids = itertools.count(1)

    @classmethod
    async def open(cls, session: aiohttp.ClientSession, url: str, token: str) -> "Connection":
        ws = await session.ws_connect(url.rstrip("/") + "/api/websocket", heartbeat=30)
        try:
            hello = await ws.receive_json(timeout=10)
            if hello.get("type") != "auth_required":
                raise AuthFailed(f"unexpected greeting {hello.get('type')!r}")
            await ws.send_json({"type": "auth", "access_token": token})
            answer = await ws.receive_json(timeout=10)
            if answer.get("type") != "auth_ok":
                raise AuthFailed(answer.get("message") or "the token was refused")
        except BaseException:
            await ws.close()
            raise
        return cls(ws, answer.get("ha_version"))

    async def call(self, message: dict[str, Any]) -> Any:
        """Send a command and wait for its result; only for setup, before subscribing."""
        id = next(self._ids)
        await self.ws.send_json({"id": id, **message})
        while True:
            answer = await self.ws.receive_json(timeout=30)
            if answer.get("id") == id and answer.get("type") == "result":
                if not answer.get("success"):
                    raise RuntimeError(answer.get("error", {}).get("message", "failed"))
                return answer.get("result")

    async def subscribe(self, entities: list[str]) -> int:
        id = next(self._ids)
        await self.ws.send_json({"id": id, "type": "subscribe_entities", "entity_ids": entities})
        return id

    async def close(self) -> None:
        await self.ws.close()


async def list_entities(url: str, token: str) -> list[EntityInfo]:
    """The entities worth reading, with their areas, for the setup page to choose from."""
    async with aiohttp.ClientSession() as session:
        connection = await Connection.open(session, url, token)
        try:
            states = await connection.call({"type": "get_states"})
            registry = await connection.call({"type": "config/entity_registry/list"})
            devices = await connection.call({"type": "config/device_registry/list"})
            areas = await connection.call({"type": "config/area_registry/list"})
        finally:
            await connection.close()
    area_names = {a["area_id"]: a["name"] for a in areas or ()}
    device_areas = {d["id"]: d.get("area_id") for d in devices or ()}
    entity_areas: dict[str, str | None] = {}
    for entry in registry or ():
        area = entry.get("area_id") or device_areas.get(entry.get("device_id"))
        entity_areas[entry["entity_id"]] = area_names.get(area) if area else None
    out = []
    for state in states or ():
        entity_id = state["entity_id"]
        attributes = state.get("attributes", {})
        domain = entity_id.split(".", 1)[0]
        device_class = attributes.get("device_class")
        unit = attributes.get("unit_of_measurement")
        quantity = _quantity(domain, device_class, unit, entity_id)
        if domain == "climate":
            points = tuple(
                (f"{ROOT}/{entity_id}/{p}", p if p != "temperature" else "temperature")
                for p, _ in HomeAssistantPlugin._climate_points(attributes)
            )
        elif quantity is not None:
            points = ((f"{ROOT}/{OWN}{entity_id}", quantity),)
        else:
            continue
        out.append(
            EntityInfo(
                entity_id,
                attributes.get("friendly_name") or entity_id,
                domain,
                device_class,
                unit,
                entity_areas.get(entity_id),
                quantity,
                points,
            )
        )
    return sorted(out, key=lambda e: ((e.area or "~").lower(), e.name.lower()))


def _quantity(
    domain: str, device_class: str | None, unit: str | None, entity_id: str
) -> str | None:
    if domain == "binary_sensor":
        if device_class in ("window", "door", "opening"):
            return "window.open"
        return device_class if device_class in STATES else None
    if domain != "sensor":
        return None
    if any(name in entity_id for name in DEMAND_ATTRIBUTES):
        return "heat_demand"
    if device_class == "pressure":
        return "atmospheric_pressure"
    if device_class in QUANTITIES:
        return device_class
    return None


# --- the plugin -------------------------------------------------------------------------


@dataclass
class State:
    value: str
    attributes: dict[str, Any]
    t: float | None
    """When HA last heard from the device, seconds since the epoch."""


class HomeAssistantPlugin:
    name = "homeassistant"
    version = "0.1.0"
    features: tuple[str, ...] = ("subscribe",)

    def __init__(
        self,
        settings: HomeAssistant,
        *,
        secrets: SecretStore | None = None,
        health_interval_s: float = 10.0,
    ) -> None:
        self.settings = settings
        self._secrets = secrets
        self._health_interval = health_interval_s
        self.states: dict[str, State] = {}
        self._ready: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._ready.add_done_callback(lambda f: f.cancelled() or f.exception())
        self._tick = asyncio.Event()
        self._connection: Connection | None = None
        self._last_traffic: datetime | None = None
        self._problem: str | None = None

    # --- the plugin interface -----------------------------------------------------------

    async def events(self, send: Send) -> None:
        try:
            token = await self._token()
            async with aiohttp.ClientSession() as session:
                self._connection = await Connection.open(session, self.settings.url, token)
                try:
                    if not self.settings.entities:
                        # Nothing chosen yet. HA would take an empty list as "everything".
                        self._ready.set_result(None)
                        while True:
                            await send(self._health())
                            await asyncio.sleep(self._health_interval)
                    subscription = await self._connection.subscribe(list(self.settings.entities))
                    reader = asyncio.create_task(self._read(subscription))
                    try:
                        while not reader.done():
                            await send(self._health())
                            await asyncio.wait({reader}, timeout=self._health_interval)
                        reader.result()
                    finally:
                        reader.cancel()
                        with contextlib.suppress(asyncio.CancelledError):
                            await reader
                finally:
                    await self._connection.close()
                    self._connection = None
        except AuthFailed as e:
            self._problem = f"Home Assistant refused the token ({e}); make a new long-lived token"
            await send(self._health())
            self._fail(e)
            raise
        except Exception as e:
            await send(self._health())
            self._fail(e)
            raise

    async def handle(self, request: Message, send: Send) -> None:
        if isinstance(request, Act):
            await send(
                Fate(
                    id=request.id,
                    stage="dropped",
                    t=_now(),
                    detail="Thermaestro only reads Home Assistant",
                )
            )
            return
        if not isinstance(request, Describe | Read | Subscribe):
            await send(
                Error(id=getattr(request, "id", None), code="unsupported", detail="not offered")
            )
            return
        await asyncio.shield(self._ready)
        if isinstance(request, Describe):
            await send(self.describe(request.id))
        elif isinstance(request, Read):
            await send(Values(id=request.id, values=tuple(await self._read_points(request))))
        else:
            await self._subscription(request, send)

    # --- reading ------------------------------------------------------------------------

    async def _token(self) -> str:
        if self._secrets is None:
            raise AuthFailed("no secrets to take the token from")
        secret = await self._secrets.get(self.settings.token)
        if secret is None:
            raise AuthFailed(f"the token {self.settings.token!r} isn't in the secrets")
        return secret.get_secret_value()

    async def _read(self, subscription: int) -> None:
        assert self._connection is not None  # noqa: S101 - set before the reader starts
        ws = self._connection.ws
        async for message in ws:
            if message.type != aiohttp.WSMsgType.TEXT:
                if message.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                    break
                continue
            data = message.json()
            if data.get("id") != subscription:
                continue
            if data.get("type") == "result" and not data.get("success"):
                raise RuntimeError(data.get("error", {}).get("message", "subscribing failed"))
            if data.get("type") == "event":
                self._apply(data.get("event", {}))
        raise ConnectionError("Home Assistant closed the connection")

    def _apply(self, event: dict[str, Any]) -> None:
        """A `subscribe_entities` event: added (a), changed (c, with + and -) or removed (r)."""
        self._last_traffic = _now()
        for entity, full in event.get("a", {}).items():
            self.states[entity] = State(
                str(full.get("s", "")), dict(full.get("a", {})), _when(full)
            )
        for entity, diff in event.get("c", {}).items():
            state = self.states.get(entity)
            if state is None:
                continue
            plus, minus = diff.get("+", {}), diff.get("-", {})
            if "s" in plus:
                state.value = str(plus["s"])
            state.attributes.update(plus.get("a", {}))
            for name in minus.get("a", ()):
                state.attributes.pop(name, None)
            state.t = _when(plus) or state.t
        for entity in event.get("r", ()):
            self.states.pop(entity, None)
        if not self._ready.done():
            self._ready.set_result(None)
        tick, self._tick = self._tick, asyncio.Event()
        tick.set()

    async def _read_points(self, request: Read) -> list[Envelope]:
        """A read; with `after`, only states HA heard after it. HA can't be asked to read
        a device, so this waits a little for a newer state, and otherwise says so."""
        if request.after is None:
            return [self.envelope(p) for p in request.points]
        after = request.after.timestamp()

        def fresh() -> bool:
            observed = (self.envelope(p).t_observed for p in request.points)
            return all(t is None or t.timestamp() >= after for t in observed)

        with contextlib.suppress(TimeoutError):
            async with asyncio.timeout(READ_AFTER_WAIT_S):
                while not fresh():
                    await self._tick.wait()
        out = []
        for path in request.points:
            e = self.envelope(path)
            if e.t_observed is not None and e.t_observed.timestamp() < after:
                e = _envelope(path, None, None, "unknown", "Home Assistant has nothing newer", None)
            out.append(e)
        return out

    async def _subscription(self, request: Subscribe, send: Send) -> None:
        sent: dict[str, tuple[object, ...]] = {}
        while True:
            values = []
            for path in request.points:
                envelope = self.envelope(path)
                state = (envelope.value, envelope.quality, envelope.why, envelope.t_observed)
                if not request.on_change or sent.get(path) != state:
                    values.append(envelope)
                    sent[path] = state
            if values:
                await send(Update(id=request.id, values=tuple(values)))
            await self._tick.wait()

    def _fail(self, error: Exception) -> None:
        if not self._ready.done():
            self._ready.set_exception(error)

    # --- describing and values ----------------------------------------------------------

    def describe(self, id: int | None = None) -> Described:
        nodes = [
            Node(
                path=ROOT, kind="site", presence=Presence(how="configured"), label="Home Assistant"
            )
        ]
        points = []
        for entity in self.settings.entities:
            state = self.states.get(entity)
            attributes = state.attributes if state else {}
            name = attributes.get("friendly_name") or entity
            domain = entity.split(".", 1)[0]
            if domain == "climate":
                nodes.append(
                    Node(
                        path=f"{ROOT}/{entity}",
                        kind="room",
                        presence=Presence(how="configured"),
                        label=name,
                    )
                )
                for point, unit in self._climate_points(attributes):
                    points.append(_point(f"{ROOT}/{entity}/{point}", unit, None))
            else:
                unit = attributes.get("unit_of_measurement")
                stored = UNITS[unit][0] if unit in UNITS else unit
                points.append(_point(f"{ROOT}/{OWN}{entity}", stored, name))
        return Described(id=id, nodes=tuple(nodes), points=tuple(points))

    @staticmethod
    def _climate_points(attributes: dict[str, Any]) -> list[tuple[str, str | None]]:
        out: list[tuple[str, str | None]] = [
            ("temperature", "degC"),
            ("setpoint", "degC"),
            ("zone.open", None),
        ]
        if any(a in attributes for a in DEMAND_ATTRIBUTES):
            out.append(("heat_demand", "%"))
        if any(a in attributes for a in WINDOW_ATTRIBUTES):
            out.append(("window.open", None))
        return out

    def envelope(self, path: str) -> Envelope:
        prefix = f"{ROOT}/"
        rest = path[len(prefix) :] if path.startswith(prefix) else ""
        entity, _, part = rest.removeprefix(OWN).partition("/")
        state = self.states.get(entity)
        if entity not in self.settings.entities or state is None:
            return _envelope(path, None, None, "unknown", "Home Assistant hasn't sent it", None)
        if state.value in MISSING_STATES:
            return _envelope(
                path, None, None, "unknown", f"Home Assistant: {state.value or 'no state'}", state.t
            )
        if part:
            return self._climate_value(path, part, state)
        if entity.startswith("binary_sensor."):
            return _envelope(path, state.value == "on", None, "good", None, state.t)
        unit = state.attributes.get("unit_of_measurement")
        try:
            number = float(state.value)
        except ValueError:
            return _envelope(path, state.value, None, "good", None, state.t)
        if unit in UNITS:
            stored, convert = UNITS[unit]
            return _envelope(path, round(convert(number), 3), stored, "good", None, state.t)
        return _envelope(path, number, unit, "good", None, state.t)

    def _climate_value(self, path: str, part: str, state: State) -> Envelope:
        a = state.attributes
        unit = a.get("temperature_unit") or "\N{DEGREE SIGN}C"
        convert = UNITS.get(unit, ("degC", lambda v: v))[1]
        value: float | bool | None
        if part == "temperature":
            value = _number(a.get("current_temperature"), convert)
        elif part == "setpoint":
            value = _number(a.get("temperature"), convert)
        elif part == "zone.open":
            action = a.get("hvac_action")
            value = None if action is None else action == "heating"
        elif part == "heat_demand":
            raw = next((a[k] for k in DEMAND_ATTRIBUTES if k in a), None)
            value = _number(raw, lambda v: v)
        elif part == "window.open":
            raw = next((a[k] for k in WINDOW_ATTRIBUTES if k in a), None)
            value = None if raw is None else raw in (True, "on", "open", "true")
        else:
            value = None
        if value is None:
            return _envelope(path, None, None, "unknown", "the device doesn't report it", state.t)
        stored = (
            "degC"
            if part in ("temperature", "setpoint")
            else "%"
            if part == "heat_demand"
            else None
        )
        return _envelope(path, value, stored, "good", None, state.t)

    def _health(self) -> Health:
        connected = self._connection is not None and not self._connection.ws.closed
        return Health(
            t=_now(),
            unit=ROOT,
            state="up" if connected else "down",
            last_traffic=self._last_traffic,
            needs_user_action=self._problem,
        )


def _point(path: str, unit: str | None, label: str | None) -> Point:
    return Point(
        path=path,
        label=label,
        unit=unit,
        delivery=Delivery(how="on_change"),
        freshness_s=Knowledge(
            value=FRESHNESS_S, known="documented", basis="Home Assistant sends changes only"
        ),
    )


def _number(raw: Any, convert: Callable[[float], float]) -> float | None:
    try:
        return round(convert(float(raw)), 3)
    except (TypeError, ValueError):
        return None


def _when(compressed: dict[str, Any]) -> float | None:
    for key in ("lr", "lu", "lc"):
        if isinstance(compressed.get(key), int | float):
            return float(compressed[key])
    return None


def _envelope(
    path: str,
    value: float | bool | str | None,
    unit: str | None,
    quality: str,
    why: str | None,
    t: float | None,
) -> Envelope:
    return Envelope.model_validate(
        {
            "point": path,
            "value": value,
            "unit": unit if value is not None else None,
            "t_observed": datetime.fromtimestamp(t, UTC) if t is not None else None,
            "t_received": _now(),
            "quality": quality,
            "source": "measured",
            "why": why,
        }
    )


def _now() -> datetime:
    return datetime.now(UTC)


def create(context: PluginContext) -> HomeAssistantPlugin:
    """The entry point: a plugin instance from its settings."""
    return HomeAssistantPlugin(
        HomeAssistant.model_validate(dict(context.settings)), secrets=context.secrets
    )
