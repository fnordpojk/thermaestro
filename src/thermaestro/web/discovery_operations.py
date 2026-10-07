"""Home Assistant discovery: the setting, and what is published; the operations behind
the settings form and the API."""

from typing import TYPE_CHECKING, Any

from ..core.discovery import new_id
from ..store import Discovery

if TYPE_CHECKING:
    from ..core.audit import AuditLog
    from ..core.discovery import Publisher
    from ..store import Database
    from .operations import Caller


class DiscoveryOperations:
    """Mixed into the services, whose parts it uses."""

    if TYPE_CHECKING:
        db: Database
        audit: AuditLog
        discovery: Publisher | None

        def _require(self, caller: Caller, permission: str, *, step_up: bool = False) -> None: ...

    async def discovery_settings(self, caller: "Caller") -> Discovery:
        caller.principal.require("settings.read")
        return await self.db.get(Discovery) or Discovery()

    async def set_discovery(self, caller: "Caller", body: dict[str, Any]) -> Discovery:
        """Switch discovery on or off, or change its topics. The installation's id is made
        the first time it's switched on, and kept."""
        self._require(caller, "settings.write")
        from .operations import _validated

        before = await self.db.get(Discovery)
        made = {k: v for k, v in body.items() if k != "id"}
        made["id"] = before.id if before is not None and before.id else None
        if made.get("enabled") and made["id"] is None:
            made["id"] = new_id()
        setting = _validated(Discovery, made, "Home Assistant discovery")
        await self.db.put(setting)
        await self.audit.record(
            caller.principal.name,
            "setting.change",
            source=caller.source,
            details={"kind": "discovery", "enabled": setting.enabled},
        )
        if self.discovery is not None:
            self.discovery.changed(before, setting)
        return setting

    def discovery_state(self, caller: "Caller") -> dict[str, Any]:
        """What is published now: off, clearing, or each device with its entities."""
        caller.principal.require("settings.read")
        publisher = self.discovery
        if publisher is None:
            return {"state": "off", "devices": []}
        model = publisher.model if publisher.state == "publishing" else None
        devices = []
        for object_id, device in sorted(
            (model.devices if model else {}).items(), key=lambda kv: kv[1].name.lower()
        ):
            entities = (
                [e for e in model.entities.values() if e.device == object_id] if model else []
            )
            if not entities:
                continue
            devices.append(
                {
                    "name": device.name,
                    "entities": len(entities),
                    "enabled": sum(1 for e in entities if e.enabled),
                }
            )
        return {"state": publisher.state, "devices": devices}
