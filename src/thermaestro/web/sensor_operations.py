"""Sensors, rooms, the outdoor references, the household's own names, the MQTT broker and
Home Assistant: the operations behind their pages and API, each checking its right.

After a change the sensor hub reads its settings again and the MQTT client starts over,
so a change takes effect at once.
"""

import re
from typing import TYPE_CHECKING, Any

from ..auth import AccountError
from ..store import (
    HomeAssistant,
    Mqtt,
    Names,
    Outdoor,
    Plugin,
    Room,
    Sensor,
)

if TYPE_CHECKING:
    from ..core.audit import AuditLog
    from ..core.host import PluginHost
    from ..core.mqtt import MqttClient
    from ..core.sensors import SensorHub
    from ..store import Database, SecretStore
    from .operations import Caller

HA_PLUGIN = "homeassistant"
ID = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")


def slug(name: str, taken: set[str]) -> str:
    """An id made from a name: `Living room` becomes `living-room`, unique."""
    base = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:56] or "x"
    id, n = base, 2
    while id in taken:
        id, n = f"{base}-{n}", n + 1
    return id


class SensorOperations:
    """Mixed into the services, whose parts it uses."""

    if TYPE_CHECKING:
        db: Database
        audit: AuditLog
        secrets: SecretStore
        host: PluginHost | None
        sensors: SensorHub | None
        mqtt: MqttClient | None

        def _require(self, caller: Caller, permission: str, *, step_up: bool = False) -> None: ...

        async def set_secret(self, caller: Caller, name: str, value: str) -> None: ...

    # --- sensors -------------------------------------------------------------------------

    async def sensor_settings(self, caller: "Caller") -> dict[str, Sensor]:
        caller.principal.require("settings.read")
        return await self.db.all(Sensor)

    async def set_sensor(
        self, caller: "Caller", id: str | None, body: dict[str, Any]
    ) -> tuple[str, Sensor]:
        """Add a sensor (no id: one is made from its name) or change one."""
        self._require(caller, "settings.write")
        from .operations import _validated

        sensor = _validated(Sensor, body, "the sensor")
        existing = await self.db.all(Sensor)
        if id is None:
            id = slug(sensor.name, set(existing))
        elif not ID.match(id):
            raise AccountError("a sensor's id is 1 to 64 letters, digits or . _ -")
        if sensor.room is not None and await self.db.get(Room, sensor.room) is None:
            raise AccountError(f"no room {sensor.room!r}")
        await self.db.put(sensor, id)
        await self.audit.record(
            caller.principal.name,
            "setting.change",
            source=caller.source,
            details={"kind": "sensor", "id": id},
        )
        await self._sensors_changed()
        return id, sensor

    async def delete_sensor(self, caller: "Caller", id: str) -> None:
        self._require(caller, "settings.write")
        if not await self.db.delete(Sensor, id):
            raise AccountError(f"no sensor {id!r}")
        outdoor = await self.db.get(Outdoor)
        if outdoor and id in outdoor.references.values():
            kept = {q: s for q, s in outdoor.references.items() if s != id}
            await self.db.put(Outdoor(references=kept))
        await self.audit.record(
            caller.principal.name,
            "setting.delete",
            source=caller.source,
            details={"kind": "sensor", "id": id},
        )
        await self._sensors_changed()

    # --- rooms ---------------------------------------------------------------------------

    async def room_settings(self, caller: "Caller") -> dict[str, Room]:
        caller.principal.require("settings.read")
        return await self.db.all(Room)

    async def set_room(
        self, caller: "Caller", id: str | None, body: dict[str, Any]
    ) -> tuple[str, Room]:
        self._require(caller, "settings.write")
        from .operations import _validated

        room = _validated(Room, body, "the room")
        existing = await self.db.all(Room)
        if id is None:
            id = slug(room.name, set(existing))
        elif not ID.match(id):
            raise AccountError("a room's id is 1 to 64 letters, digits or . _ -")
        await self.db.put(room, id)
        await self.audit.record(
            caller.principal.name,
            "setting.change",
            source=caller.source,
            details={"kind": "room", "id": id},
        )
        await self._sensors_changed()
        return id, room

    async def delete_room(self, caller: "Caller", id: str) -> None:
        """Remove a room; its sensors stay, without a room."""
        self._require(caller, "settings.write")

        def delete(t: Any) -> bool:
            if not t.delete(Room, id):
                return False
            for sensor_id, sensor in t.all(Sensor).items():
                if sensor.room == id:
                    t.put(sensor.model_copy(update={"room": None, "reference": False}), sensor_id)
            return True

        if not await self.db.run(delete):
            raise AccountError(f"no room {id!r}")
        await self.audit.record(
            caller.principal.name,
            "setting.delete",
            source=caller.source,
            details={"kind": "room", "id": id},
        )
        await self._sensors_changed()

    # --- the outdoor references ----------------------------------------------------------

    async def outdoor(self, caller: "Caller") -> Outdoor:
        caller.principal.require("settings.read")
        return await self.db.get(Outdoor) or Outdoor()

    async def set_outdoor(self, caller: "Caller", references: dict[str, str]) -> Outdoor:
        self._require(caller, "settings.write")
        from .operations import _validated

        chosen = {q: s for q, s in references.items() if s}
        outdoor = _validated(Outdoor, {"references": chosen}, "the outdoor references")
        sensors = await self.db.all(Sensor)
        for quantity, id in outdoor.references.items():
            sensor = sensors.get(id)
            if sensor is None or sensor.placement != "outdoor" or sensor.quantity != quantity:
                raise AccountError(f"{id!r} isn't an outdoor {quantity} sensor")
        await self.db.put(outdoor)
        await self.audit.record(
            caller.principal.name,
            "setting.change",
            source=caller.source,
            details={"kind": "outdoor"},
        )
        await self._sensors_changed()
        return outdoor

    # --- names ---------------------------------------------------------------------------

    async def names(self, caller: "Caller") -> dict[str, str]:
        caller.principal.require("points.read")
        found = await self.db.get(Names)
        return dict(found.names) if found else {}

    async def set_name(self, caller: "Caller", ref: str, name: str | None) -> None:
        """Name a point or node (`<instance>:<path>`) for everyone; empty: the built-in."""
        self._require(caller, "settings.write")
        from .operations import _validated

        current = await self.db.get(Names)
        names = dict(current.names) if current else {}
        if name and name.strip():
            names[ref] = name.strip()
        else:
            names.pop(ref, None)
        await self.db.put(_validated(Names, {"names": names}, "the name"))
        await self.audit.record(
            caller.principal.name,
            "setting.change",
            source=caller.source,
            details={"kind": "name", "of": ref},
        )
        await self._sensors_changed()

    # --- the MQTT broker -----------------------------------------------------------------

    async def mqtt_settings(self, caller: "Caller") -> Mqtt | None:
        caller.principal.require("settings.read")
        return await self.db.get(Mqtt)

    async def set_mqtt(self, caller: "Caller", body: dict[str, Any]) -> Mqtt:
        self._require(caller, "settings.write")
        from .operations import _validated

        mqtt = _validated(Mqtt, body, "the MQTT broker")
        await self.db.put(mqtt)
        await self.audit.record(
            caller.principal.name, "setting.change", source=caller.source, details={"kind": "mqtt"}
        )
        if self.mqtt is not None:
            self.mqtt.reload()
        return mqtt

    def mqtt_state(self, caller: "Caller") -> dict[str, str | None]:
        caller.principal.require("settings.read")
        if self.mqtt is None:
            return {"state": "off", "error": None}
        return {"state": self.mqtt.state, "error": self.mqtt.error}

    # --- Home Assistant ------------------------------------------------------------------

    async def set_home_assistant(
        self, caller: "Caller", id: str, url: str, token_name: str
    ) -> Plugin:
        """Add or change the connection; the entities chosen so far are kept."""
        existing = await self.db.get(Plugin, id)
        entities = existing.settings.get("entities", []) if existing else []
        settings = {"url": url.strip().rstrip("/"), "token": token_name, "entities": entities}
        return await self._put_home_assistant(caller, id, settings)

    async def home_assistant_entities(self, caller: "Caller", id: str) -> list[Any]:
        """What the Home Assistant offers to read, for choosing."""
        caller.principal.require("settings.read")
        from ..homeassistant.plugin import list_entities

        settings = await self._home_assistant(id)
        token = await self.secrets.get(settings.token)
        if token is None:
            raise AccountError("enter the Home Assistant token first")
        try:
            return await list_entities(settings.url, token.get_secret_value())
        except Exception as e:  # noqa: BLE001 - shown to the user, whatever went wrong
            raise AccountError(f"Home Assistant didn't answer: {e}") from None

    async def add_home_assistant_sensors(
        self, caller: "Caller", id: str, picks: list[dict[str, Any]]
    ) -> list[str]:
        """Read the picked entities, and make a sensor of each of their points:
        `{"entity": ..., "point": ..., "quantity": ..., "name": ..., "room": ...}`. Instead
        of `room`, `new_room` names a room to make (from the entity's area), or to use where
        one of that name exists."""
        self._require(caller, "settings.write")
        settings = await self._home_assistant(id)
        entities = list(settings.entities)
        for pick in picks:
            if pick["entity"] not in entities:
                entities.append(pick["entity"])
        await self._put_home_assistant(caller, id, {**settings.model_dump(), "entities": entities})
        new = {str(p["new_room"]) for p in picks if p.get("new_room") and not p.get("room")}
        rooms = await self._rooms_named(caller, sorted(new))
        made = []
        for pick in picks:
            room = pick.get("room") or rooms.get(str(pick.get("new_room") or "").strip().lower())
            placement = "room" if room else pick.get("placement", "other")
            body = {
                "name": pick["name"],
                "source": "point",
                "point": f"{id}:{pick['point']}",
                "quantity": pick["quantity"],
                "placement": placement,
                "room": room or None,
            }
            sensor_id, _ = await self.set_sensor(caller, None, body)
            made.append(sensor_id)
        return made

    async def _rooms_named(self, caller: "Caller", names: list[str]) -> dict[str, str]:
        """The room of each name, made where none has it; by name in lower case. A room made
        this way is Thermaestro's own: renaming the area later doesn't change it."""
        existing = {r.name.lower(): rid for rid, r in (await self.db.all(Room)).items()}
        out = {}
        for name in names:
            key = name.strip().lower()
            if not key:
                continue
            if key not in existing:
                existing[key], _ = await self.set_room(caller, None, {"name": name.strip()})
            out[key] = existing[key]
        return out

    async def home_assistant_areas(self, caller: "Caller", id: str) -> list[str]:
        """The areas of Home Assistant's entities that aren't a room yet, by name."""
        entities = await self.home_assistant_entities(caller, id)
        rooms = {r.name.lower() for r in (await self.db.all(Room)).values()}
        areas = {e.area.strip() for e in entities if e.area and e.area.strip()}
        return sorted((a for a in areas if a.lower() not in rooms), key=str.lower)

    async def rooms_from_areas(self, caller: "Caller", id: str, areas: list[str]) -> list[str]:
        """Make a room of each area named, where it isn't one yet; the rooms' ids."""
        self._require(caller, "settings.write")
        offered = {a.lower() for a in await self.home_assistant_areas(caller, id)}
        unknown = [a for a in areas if a.lower() not in offered]
        if unknown:
            raise AccountError(f"Home Assistant has no area {unknown[0]!r} without a room")
        return list((await self._rooms_named(caller, areas)).values())

    async def _home_assistant(self, id: str) -> HomeAssistant:
        plugin = await self.db.get(Plugin, id)
        if plugin is None or plugin.plugin != HA_PLUGIN:
            raise AccountError(f"no Home Assistant connection {id!r}")
        return plugin.typed(HomeAssistant)

    async def _put_home_assistant(
        self, caller: "Caller", id: str, settings: dict[str, Any]
    ) -> Plugin:
        self._require(caller, "plugins.manage", step_up=True)
        from .operations import _validated

        if not ID.match(id):
            raise AccountError("an instance name is 1 to 64 letters, digits or . _ -")
        plugin = _validated(Plugin, {"plugin": HA_PLUGIN, "settings": settings}, "Home Assistant")
        await self.db.put(plugin, id)
        await self.audit.record(
            caller.principal.name,
            "plugin.change",
            source=caller.source,
            details={"instance": id, "plugin": HA_PLUGIN},
        )
        if self.host is not None:
            await self.host.apply(id)
        return plugin

    async def _sensors_changed(self) -> None:
        if self.sensors is not None:
            await self.sensors.load()
        if self.mqtt is not None:
            self.mqtt.reload()
