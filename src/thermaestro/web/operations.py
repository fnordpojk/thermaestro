"""What the web UI and the API can do, each operation checking its right first.

Both surfaces call these functions and nothing else, so a page can't do what the API
can't, and neither can skip a check. An operation under a step-up right (users, groups,
secrets, plugins) also needs the password entered within the last 15 minutes when the
request comes from a browser session; a token carries rights its user granted it after
entering the password, so it needs no step-up of its own.
"""

import json
import math
from collections.abc import Collection
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta, tzinfo
from pathlib import Path
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ValidationError

if TYPE_CHECKING:
    from ..core.executor import Executor
    from ..intents import Intents
    from ..planner import Planner

from ..auth import (
    AccountError,
    Accounts,
    Preferences,
    Principal,
    Session,
    SessionInfo,
    SetupCode,
    TokenInfo,
    User,
)
from ..auth.permissions import PERMISSIONS
from ..cap.messages import Described
from ..core.audit import AuditLog
from ..core.discovery import Publisher
from ..core.host import PluginHost
from ..core.mqtt import MqttClient
from ..core.sensors import SENSORS, SITE, SensorHub
from ..core.series import Series
from ..core.values import Key, Values, sample
from ..core.weather import Weather
from ..store import Database, Location, NibeGateway, Plugin, SecretStore
from ..store.errors import name_fields
from . import i18n, labels
from .control_operations import ControlOperations
from .discovery_operations import DiscoveryOperations
from .price_operations import PriceOperations
from .sensor_operations import SensorOperations
from .weather_operations import WeatherOperations

HISTORY_MAX_S = 31 * 86_400.0
TOPIC_OF_PLUGIN = {
    "nibe": "/setup/pump",
    "homeassistant": "/setup/external",
    "tibber": "/setup/prices",
    "entsoe": "/setup/prices",
    "met_norway": "/setup/weather",
    "smhi": "/setup/weather",
    "open_meteo": "/setup/weather",
}
"""Where under Setup each plugin is set up, for "Needs attention"."""
ROOM_CARD_HIDDEN = ("absolute_humidity", "dew_point")
"""Derived from a room's temperature and humidity: kept, charted and in the API, but more
than the overview's room cards need."""
"""The longest stretch of samples one request returns."""


class NeedsConfirmation(AccountError):
    def __init__(self) -> None:
        super().__init__("enter your password again to make this change")


class NotFound(AccountError):
    pass


@dataclass
class Caller:
    """Who is asking, and how: a session from a browser, or a token."""

    principal: Principal
    session: Session | None = None
    source: str | None = None


@dataclass
class Services(
    SensorOperations, PriceOperations, WeatherOperations, DiscoveryOperations, ControlOperations
):
    accounts: Accounts
    db: Database
    values: Values
    host: PluginHost | None
    audit: AuditLog
    secrets: SecretStore
    setup: SetupCode
    fingerprint: str | None = None
    """The self-signed certificate's SHA-256, shown so a user can compare it."""
    zone: tzinfo = field(default=UTC)
    """The house's time zone, from the location; every time is shown in it."""
    sensors: SensorHub | None = None
    mqtt: MqttClient | None = None
    series: Series | None = None
    weather: Weather | None = None
    discovery: Publisher | None = None
    intents: "Intents | None" = None
    executor: "Executor | None" = None
    planner: "Planner | None" = None

    async def load_zone(self) -> None:
        location = await self.db.get(Location)
        self.zone = ZoneInfo(location.timezone) if location else UTC

    def _require(self, caller: Caller, permission: str, *, step_up: bool = False) -> None:
        caller.principal.require(permission)
        if step_up:
            self._require_confirmed(caller)

    # --- status --------------------------------------------------------------------------

    def status(self, caller: Caller) -> list[dict[str, Any]]:
        """Every plugin instance, its state and health, and its points' latest values."""
        caller.principal.require("points.read")
        out = []
        instances = self.host.instances if self.host else {}
        for id, instance in sorted(instances.items()):
            points = []
            described = instance.described
            for point in described.points if described else ():
                envelope = self.values.latest.get(Key(id, point.path))
                points.append(
                    {
                        "path": point.path,
                        "label": self._label(id, described, point.path),
                        "category": self._category(id, point.path, point.category),
                        "unit": point.unit,
                        "digits": digits(
                            point.resolution.value, envelope.value if envelope else None
                        ),
                        "value": None if envelope is None else envelope.value,
                        "quality": "unknown" if envelope is None else envelope.quality,
                        "why": None if envelope is None else envelope.why,
                        "t": None if envelope is None else sample(envelope).t,
                    }
                )
            health = instance.health
            out.append(
                {
                    "id": id,
                    "plugin": instance.setting.plugin,
                    "state": str(instance.state),
                    "failures": instance.failures,
                    "last_error": instance.last_error,
                    "health": None if health is None else health.model_dump(mode="json"),
                    "points": points,
                }
            )
        return out

    def devices(self, caller: Caller) -> list[dict[str, Any]]:
        """Each identified device, such as a heat pump, with its points by the part they
        belong to: the device itself first, then its parts in the order described."""
        caller.principal.require("points.read")
        names = self.sensors.names if self.sensors else {}
        out = []
        for item in self.status(caller):
            found = self.host.instances.get(item["id"]) if self.host else None
            described = found.described if found else None
            for node in described.nodes if described else ():
                identity = node.identity
                if identity is None:
                    continue
                shown = node.label or identity.model
                if not shown.lower().startswith(identity.vendor.lower()):
                    shown = f"{identity.vendor} {shown}"
                order = [n.path for n in described.nodes] if described else []
                parts: dict[str, list[dict[str, Any]]] = {}
                for p in item["points"]:
                    if not p["path"].startswith(f"{node.path}/"):
                        continue
                    where = p["path"].rpartition("/")[0]
                    own = {**p, "label": p["label"].rpartition(" \N{MIDDLE DOT} ")[2]}
                    parts.setdefault(where, []).append(own)
                ranked = sorted(parts, key=lambda w: order.index(w) if w in order else len(order))
                out.append(
                    {
                        "instance": item["id"],
                        "node": node.path,
                        "name": names.get(f"{item['id']}:{node.path}") or shown,
                        "identity": identity.model_dump(mode="json"),
                        "state": item["state"],
                        "health": item["health"],
                        "last_error": item["last_error"],
                        "parts": [
                            {
                                "node": where,
                                "name": None
                                if where == node.path
                                else self.node_label(caller, item["id"], where),
                                "points": parts[where],
                            }
                            for where in ranked
                        ],
                    }
                )
        return out

    def attention(self, caller: Caller) -> list[dict[str, Any]]:
        """The plugins that are down or ask for something, with where to set them up: what
        Home Assistant's "Needs attention" counts too."""
        out = []
        for item in self.status(caller):
            health = item["health"] or {}
            asks = health.get("needs_user_action")
            down = item["state"] != "up" or health.get("state", "up") != "up"
            if not (asks or down or health.get("stale")):
                continue
            out.append(
                {
                    "id": item["id"],
                    "plugin": item["plugin"],
                    "state": item["state"],
                    "why": asks or item["last_error"] or None,
                    "topic": TOPIC_OF_PLUGIN.get(item["plugin"], "/system/health"),
                }
            )
        # A device's own warnings, such as brine out near the pump's own alarm limit.
        for id, instance in sorted((self.host.instances if self.host else {}).items()):
            for event in instance.active.values():
                out.append(
                    {
                        "id": id,
                        "plugin": instance.setting.plugin,
                        "state": "warning",
                        "why": event.text,
                        "topic": "/pump",
                    }
                )
        return out

    def point_label(self, caller: Caller, instance: str, path: str) -> str:
        caller.principal.require("points.read")
        found = self.host.instances.get(instance) if self.host else None
        return self._label(instance, found.described if found else None, path)

    def point_description(self, instance: str, path: str) -> str | None:
        """What the device's documentation says the point is."""
        found = self.host.instances.get(instance) if self.host else None
        described = found.described if found else None
        point = next((p for p in described.points if p.path == path), None) if described else None
        return point.description if point else None

    def point_digits(self, instance: str, path: str) -> int:
        """The decimals a point's values are shown with, for its chart."""
        found = self.host.instances.get(instance) if self.host else None
        described = found.described if found else None
        point = next((p for p in described.points if p.path == path), None) if described else None
        latest = self.values.latest.get(Key(instance, path))
        return digits(point.resolution.value if point else None, latest.value if latest else None)

    def point_kind(self, instance: str, path: str) -> str:
        """How a point's chart draws it: "number" as a line, "state" (named values) and
        "switch" (on and off) as bands over time."""
        latest = self.values.latest.get(Key(instance, path))
        value = latest.value if latest else None
        if isinstance(value, bool):
            return "switch"
        if isinstance(value, str):
            return "state"
        if value is None:
            found = self.host.instances.get(instance) if self.host else None
            described = found.described if found else None
            point = (
                next((p for p in described.points if p.path == path), None) if described else None
            )
            if point is not None and point.enum.value:
                return "state"
        return "number"

    def label(self, instance: str, path: str) -> str:
        """A point's name as the pages show it, in the current language."""
        found = self.host.instances.get(instance) if self.host else None
        return self._label(instance, found.described if found else None, path)

    def shown_as(self, instance: str, path: str, given: str | None) -> str | None:
        """A point's category: the household's choice, else its plugin's."""
        return self._category(instance, path, given)

    def built_in_label(self, instance: str, path: str) -> str:
        found = self.host.instances.get(instance) if self.host else None
        return _label(found.described if found else None, path)

    def node_label(
        self, caller: Caller, instance: str, node: str, *, built_in: bool = False
    ) -> str:
        """A node's name: the household's, else the built-in one."""
        caller.principal.require("points.read")
        names = self.sensors.names if self.sensors and not built_in else {}
        if f"{instance}:{node}" in names:
            return names[f"{instance}:{node}"]
        found = self.host.instances.get(instance) if self.host else None
        described = found.described if found else None
        nodes = {n.path: n for n in described.nodes} if described else {}
        n = nodes.get(node)
        return labels.node(n.kind if n else None, node, n.label if n else None) or node

    def _label(self, instance: str, described: Described | None, path: str) -> str:
        if instance == SITE:
            return self._site_label(path)
        if instance == SENSORS:
            return self._sensor_label(path)
        names = self.sensors.names if self.sensors else {}
        own = names.get(f"{instance}:{path}")
        if own:
            return own
        where = path.rpartition("/")[0]
        node_name = names.get(f"{instance}:{where}")
        built_in = _label(described, path)
        if node_name:
            return f"{node_name} \N{MIDDLE DOT} {built_in.rpartition(' · ')[2]}"
        return built_in

    def _category(self, instance: str, path: str, given: str | None) -> str | None:
        """The household's choice where it made one, else the plugin's."""
        display = self.sensors.display if self.sensors else None
        chosen = display.categories.get(f"{instance}:{path}") if display else None
        if chosen is None:
            return given
        return None if chosen == "primary" else chosen

    def _pinned_cards(self) -> list[dict[str, Any]]:
        """The pinned points, on a card per node they belong to, in the order pinned."""
        display = self.sensors.display if self.sensors else None
        if display is None or self.host is None:
            return []
        cards: dict[tuple[str, str], dict[str, Any]] = {}
        for ref in display.pinned:
            instance, _, path = ref.partition(":")
            found = self.host.instances.get(instance)
            described = found.described if found else None
            point = (
                next((p for p in described.points if p.path == path), None) if described else None
            )
            if point is None:
                continue
            node = path.rpartition("/")[0]
            key = (instance, node)
            if key not in cards:
                cards[key] = {
                    "name": self._card_name(instance, described, node),
                    "instance": instance,
                    "points": [],
                }
            envelope = self.values.latest.get(Key(instance, path))
            own = self._label(instance, described, path)
            cards[key]["points"].append(
                {
                    "path": path,
                    "instance": instance,
                    "label": own.rpartition(" \N{MIDDLE DOT} ")[2],
                    "unit": point.unit,
                    "digits": digits(point.resolution.value, envelope.value if envelope else None),
                    "value": None if envelope is None else envelope.value,
                    "quality": "unknown" if envelope is None else envelope.quality,
                    "why": None if envelope is None else envelope.why,
                    "t": None if envelope is None else sample(envelope).t,
                }
            )
        return list(cards.values())

    def _card_name(self, instance: str, described: Described | None, node: str) -> str:
        names = self.sensors.names if self.sensors else {}
        if f"{instance}:{node}" in names:
            return names[f"{instance}:{node}"]
        nodes = {n.path: n for n in described.nodes} if described else {}
        n = nodes.get(node)
        if n is None:
            return instance
        return labels.node(n.kind, node, n.label) or n.label or instance

    async def set_display(
        self, caller: Caller, ref: str, category: str | None, pinned: bool | None
    ) -> None:
        """Move a point to a category (None: the plugin's) and pin it to the overview or
        not (None: leave as it is)."""
        self._require(caller, "settings.write")
        from ..store import Display

        current = await self.db.get(Display) or Display()
        categories = dict(current.categories)
        if category in (None, ""):
            categories.pop(ref, None)
        else:
            categories[ref] = category  # type: ignore[assignment]
        pins = list(current.pinned)
        if pinned is True and ref not in pins:
            pins.append(ref)
        elif pinned is False and ref in pins:
            pins.remove(ref)
        display = _validated(Display, {"categories": categories, "pinned": pins}, "the display")
        await self.db.put(display)
        await self.audit.record(
            caller.principal.name,
            "setting.change",
            source=caller.source,
            details={"kind": "display", "of": ref},
        )
        if self.sensors is not None:
            await self.sensors.load()

    def _site_label(self, path: str) -> str:
        node, _, quantity = path.rpartition("/")
        if node == "outdoor":
            place = i18n._("Outdoors")
        else:
            room = self.sensors.rooms.get(node.removeprefix("room.")) if self.sensors else None
            place = room.name if room else node
        return f"{place} \N{MIDDLE DOT} {labels.quantity(quantity)}"

    def _sensor_label(self, path: str) -> str:
        id, _, quantity = path.rpartition("/")
        sensor = self.sensors.sensors.get(id) if self.sensors else None
        return f"{sensor.name if sensor else id} · {labels.quantity(quantity)}"

    def site(self, caller: Caller) -> dict[str, Any]:
        """Rooms and the outdoors, as the core derives them from the sensors."""
        caller.principal.require("points.read")
        hub = self.sensors
        if hub is None:
            return {"rooms": [], "outdoor": [], "pinned": []}
        rooms = []
        for room_id, room in sorted(hub.rooms.items(), key=lambda r: r[1].name.lower()):
            node = f"room.{room_id}"
            points = self._site_points(node)
            rooms.append(
                {
                    "id": room_id,
                    "name": room.name,
                    "own_device": room.own_device,
                    "points": points,
                    "shown": [
                        p for p in points if p["path"].rpartition("/")[2] not in ROOM_CARD_HIDDEN
                    ],
                }
            )
        return {
            "rooms": rooms,
            "outdoor": self._site_points("outdoor"),
            "pinned": self._pinned_cards(),
        }

    def _site_points(self, node: str) -> list[dict[str, Any]]:
        out = []
        for key, envelope in sorted(self.values.latest.items(), key=lambda kv: kv[0].point):
            if key.instance != SITE or key.point.rpartition("/")[0] != node:
                continue
            quantity = key.point.rpartition("/")[2]
            out.append(
                {
                    "path": key.point,
                    "label": labels.quantity(quantity),
                    "unit": envelope.unit,
                    "digits": digits(None, envelope.value),
                    "value": envelope.value,
                    "quality": envelope.quality,
                    "why": envelope.why,
                    "t": sample(envelope).t,
                }
            )
        return out

    def sensor_readings(self, caller: Caller) -> dict[str, dict[str, Any]]:
        """Each sensor's latest value, for the sensors page."""
        caller.principal.require("points.read")
        out: dict[str, dict[str, Any]] = {}
        if self.sensors is None:
            return out
        for id, reading in self.sensors.readings():
            if reading is not None:
                out[id] = {
                    "value": reading.value,
                    "digits": digits(None, reading.value),
                    "quality": reading.quality,
                    "why": reading.why,
                    "t": reading.t,
                }
        return out

    async def history(
        self, caller: Caller, instance: str, point: str, start: float, end: float
    ) -> list[dict[str, Any]]:
        caller.principal.require("points.read")
        if not 0 < end - start <= HISTORY_MAX_S:
            raise AccountError("a history request covers up to 31 days")
        samples = await self.values.history(Key(instance, point), start, end)
        return [{"t": s.t, "value": s.value, "text": s.text, "quality": s.quality} for s in samples]

    async def daily(
        self, caller: Caller, instance: str, point: str, days: int
    ) -> list[dict[str, Any]]:
        """Each of the last `days` days' lowest, mean and highest good value."""
        caller.principal.require("points.read")
        now = datetime.now(self.zone)
        first = (now - timedelta(days=days - 1)).replace(hour=0, minute=0, second=0, microsecond=0)
        rows = await self.values.daily(
            Key(instance, point), first.timestamp(), now.timestamp() + 1, self.zone
        )
        return [
            {"day": d.isoformat(), "min": low, "mean": mean, "max": high}
            for d, low, mean, high in rows
        ]

    # --- settings ------------------------------------------------------------------------

    async def location(self, caller: Caller) -> Location | None:
        caller.principal.require("settings.read")
        return await self.db.get(Location)

    async def set_location(self, caller: Caller, body: dict[str, Any]) -> Location:
        self._require(caller, "settings.write")
        location = _validated(Location, body, "location")
        await self.db.put(location)
        self.zone = ZoneInfo(location.timezone)
        await self.audit.record(
            caller.principal.name,
            "setting.change",
            source=caller.source,
            details={"kind": "location"},
        )
        await self._weather_follows_location(caller, location)
        return location

    async def plugins(self, caller: Caller) -> dict[str, Plugin]:
        caller.principal.require("settings.read")
        return await self.db.all(Plugin)

    async def set_pump(self, caller: Caller, id: str, body: dict[str, Any]) -> Plugin:
        """Add or change a Nibe pump's connection, and (re)start its plugin instance."""
        self._require(caller, "plugins.manage", step_up=True)
        gateway = _validated(NibeGateway, body, "the pump connection")
        setting = _validated(
            Plugin, {"plugin": "nibe", "settings": gateway.model_dump(mode="json")}, "plugin"
        )
        if not id or len(id) > 64 or not all(c.isalnum() or c in "_.-" for c in id):
            raise AccountError("an instance name is 1 to 64 letters, digits or . _ -")
        await self.db.put(setting, id)
        await self.audit.record(
            caller.principal.name,
            "plugin.change",
            source=caller.source,
            details={"instance": id, "plugin": "nibe"},
        )
        if self.host is not None:
            await self.host.apply(id)
        return setting

    async def set_secret(self, caller: Caller, name: str, value: str) -> None:
        """Enter or replace a secret; it is never shown again."""
        self._require(caller, "secrets.manage", step_up=True)
        if not value:
            raise AccountError("a secret can't be empty")
        try:
            await self.secrets.set(name, value)
        except ValueError as e:
            raise AccountError(str(e)) from None
        await self.audit.record(
            caller.principal.name, "secret.set", source=caller.source, details={"name": name}
        )

    async def secret_names(self, caller: Caller) -> list[str]:
        caller.principal.require("settings.read")
        return await self.secrets.names()

    def logset(self, caller: Caller, model_name: str) -> bytes:
        """A LOG.SET for a bus-family Nibe pump, to copy to a USB stick."""
        caller.principal.require("settings.read")
        from ..nibe import logset, profile
        from ..nibe.maps import load

        try:
            model = load("bus").model(model_name)
        except KeyError:
            raise NotFound(f"no register map for {model_name!r}") from None
        registers = [r for r in profile.LOG_SET if r in model]
        return logset.render(model, registers, day=date.today())

    # --- users and groups ----------------------------------------------------------------

    async def users(self, caller: Caller) -> list[User]:
        caller.principal.require("users.manage")
        return await self.accounts.users()

    async def groups(self, caller: Caller) -> dict[str, frozenset[str]]:
        caller.principal.require("users.manage")
        return await self.accounts.groups()

    async def create_user(
        self, caller: Caller, name: str, password: str, groups: Collection[str]
    ) -> User:
        self._require(caller, "users.manage", step_up=True)
        return await self.accounts.create_user(
            name, password, groups, by=caller.principal.name, source=caller.source
        )

    async def set_user_groups(self, caller: Caller, name: str, groups: Collection[str]) -> None:
        self._require(caller, "users.manage", step_up=True)
        await self.accounts.set_groups(name, groups, by=caller.principal.name, source=caller.source)

    async def set_user_password(
        self, caller: Caller, name: str, password: str, *, end_sessions: bool = True
    ) -> None:
        """Another user's password, by an administrator; one's own needs only a step-up."""
        if name.lower() != caller.principal.user.name.lower():
            self._require(caller, "users.manage", step_up=True)
        else:
            self._require_confirmed(caller)
        await self.accounts.set_password(
            name,
            password,
            by=caller.principal.name,
            source=caller.source,
            end_sessions=end_sessions,
        )

    async def delete_user(self, caller: Caller, name: str) -> None:
        self._require(caller, "users.manage", step_up=True)
        await self.accounts.delete_user(name, by=caller.principal.name, source=caller.source)

    async def set_group(self, caller: Caller, name: str, permissions: Collection[str]) -> None:
        self._require(caller, "users.manage", step_up=True)
        await self.accounts.set_group(
            name, permissions, by=caller.principal.name, source=caller.source
        )

    async def delete_group(self, caller: Caller, name: str) -> None:
        self._require(caller, "users.manage", step_up=True)
        await self.accounts.delete_group(name, by=caller.principal.name, source=caller.source)

    def _require_confirmed(self, caller: Caller) -> None:
        if caller.session is not None and not self.accounts.confirmed_recently(caller.session):
            raise NeedsConfirmation

    # --- one's own tokens and sessions ---------------------------------------------------

    async def tokens(self, caller: Caller) -> list[TokenInfo]:
        caller.principal.require("tokens.own")
        return await self.accounts.tokens(caller.principal.user)

    async def create_token(
        self, caller: Caller, name: str, permissions: Collection[str], days: int
    ) -> str:
        self._require_confirmed(caller)
        return await self.accounts.create_token(
            caller.principal, name, permissions, days=days, source=caller.source
        )

    async def revoke_token(self, caller: Caller, token_id: int) -> None:
        caller.principal.require("tokens.own")
        await self.accounts.revoke_token(caller.principal, token_id, source=caller.source)

    async def sessions(self, caller: Caller, user: str | None = None) -> list[SessionInfo]:
        """One's own sessions, or with users.manage, another user's."""
        if user is None or user.lower() == caller.principal.user.name.lower():
            return await self.accounts.sessions(caller.principal.user)
        caller.principal.require("users.manage")
        other = await self.accounts.user(user)
        if other is None:
            raise NotFound(f"no user {user!r}")
        return await self.accounts.sessions(other)

    async def end_session(self, caller: Caller, user: str, session_id: str) -> None:
        await self.accounts.end_session_by_id(
            caller.principal, user, session_id, source=caller.source
        )

    # --- one's own language and formats ------------------------------------------------

    def preferences(self, caller: Caller) -> Preferences:
        return caller.principal.user.preferences

    async def set_preferences(self, caller: Caller, body: dict[str, Any]) -> Preferences:
        preferences = _validated(Preferences, body, "the language and formats")
        await self.accounts.set_preferences(caller.principal.user, preferences)
        return preferences

    # --- the audit log -------------------------------------------------------------------

    async def audit_entries(self, caller: Caller, limit: int = 200) -> list[dict[str, Any]]:
        """The newest entries first."""
        caller.principal.require("audit.read")
        limit = max(1, min(limit, 2000))
        return _tail(self.audit.path, limit)


def digits(resolution: float | None, value: object) -> int:
    """How many decimals a value is shown with: as many as its point's resolution has
    (1 for whole numbers, 0.1 for tenths), else none for a whole number and one for
    anything else."""
    if resolution is not None and resolution > 0:
        return 0 if resolution >= 1 else min(3, max(0, round(-math.log10(resolution))))
    if isinstance(value, int) or (isinstance(value, float) and value.is_integer()):
        return 0
    return 1


def _label(described: Described | None, path: str) -> str:
    """A point's name for people, with the node it sits on: "Climate system 1 · Supply
    temperature"."""
    where, _, own = path.rpartition("/")
    nodes = {n.path: n for n in described.nodes} if described else {}
    points = {p.path: p for p in described.points} if described else {}
    node = nodes.get(where)
    point = points.get(path)
    place = labels.node(node.kind if node else None, where, node.label if node else None)
    name = labels.point(own, point.label if point else None)
    return f"{place} \N{MIDDLE DOT} {name}" if place else name


def rights() -> dict[str, str]:
    return dict(PERMISSIONS)


def _validated[M: BaseModel](model: type[M], body: dict[str, Any], what: str) -> M:
    try:
        return model.model_validate(body)
    except ValidationError as e:
        raise AccountError(str(name_fields(what, e))) from None


def _tail(path: Path, limit: int) -> list[dict[str, Any]]:
    try:
        lines = path.read_bytes().splitlines()[-limit:]
    except FileNotFoundError:
        return []
    out = []
    for line in reversed(lines):
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out
