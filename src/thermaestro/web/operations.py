"""What the web UI and the API can do, each operation checking its right first.

Both surfaces call these functions and nothing else, so a page can't do what the API
can't, and neither can skip a check. An operation under a step-up right (users, groups,
secrets, plugins) also needs the password entered within the last 15 minutes when the
request comes from a browser session; a token carries rights its user granted it after
entering the password, so it needs no step-up of its own.
"""

import json
from collections.abc import Collection
from dataclasses import dataclass, field
from datetime import UTC, date, tzinfo
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ValidationError

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
from ..core.host import PluginHost
from ..core.mqtt import MqttInput
from ..core.sensors import SITE, SensorHub
from ..core.values import Key, Values, sample
from ..store import Database, Location, NibeGateway, Plugin, SecretStore
from ..store.errors import name_fields
from . import i18n, labels
from .sensor_operations import SensorOperations

HISTORY_MAX_S = 31 * 86_400.0
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
class Services(SensorOperations):
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
    mqtt: MqttInput | None = None

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
                        "unit": point.unit,
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

    def point_label(self, caller: Caller, instance: str, path: str) -> str:
        caller.principal.require("points.read")
        found = self.host.instances.get(instance) if self.host else None
        return self._label(instance, found.described if found else None, path)

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

    def _site_label(self, path: str) -> str:
        node, _, quantity = path.rpartition("/")
        if node == "outdoor":
            place = i18n._("Outdoors")
        else:
            room = self.sensors.rooms.get(node.removeprefix("room.")) if self.sensors else None
            place = room.name if room else node
        return f"{place} \N{MIDDLE DOT} {labels.quantity(quantity)}"

    def site(self, caller: Caller) -> dict[str, Any]:
        """Rooms and the outdoors, as the core derives them from the sensors."""
        caller.principal.require("points.read")
        hub = self.sensors
        if hub is None:
            return {"rooms": [], "outdoor": []}
        rooms = []
        for room_id, room in sorted(hub.rooms.items(), key=lambda r: r[1].name.lower()):
            node = f"room.{room_id}"
            rooms.append(
                {
                    "id": room_id,
                    "name": room.name,
                    "own_device": room.own_device,
                    "points": self._site_points(node),
                }
            )
        return {"rooms": rooms, "outdoor": self._site_points("outdoor")}

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
