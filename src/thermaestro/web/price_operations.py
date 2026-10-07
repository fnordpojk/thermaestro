"""Prices: the layers of the stack, VAT, the series the plugins offer, and the stack
itself per day; the operations behind the prices page and its API."""

from datetime import date
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from ..auth import AccountError
from ..core.prices import Stack, assemble
from ..store import Plugin, PriceLayer, Vat
from .sensor_operations import ID, slug

if TYPE_CHECKING:
    from datetime import tzinfo

    from ..core.audit import AuditLog
    from ..core.host import PluginHost
    from ..core.series import Series
    from ..store import Database
    from .operations import Caller

PRICE_PLUGINS = ("tibber", "entsoe")


class PriceOperations:
    """Mixed into the services, whose parts it uses."""

    if TYPE_CHECKING:
        db: Database
        audit: AuditLog
        series: Series | None
        host: PluginHost | None
        zone: tzinfo

        def _require(self, caller: Caller, permission: str, *, step_up: bool = False) -> None: ...

    async def price_layers(self, caller: "Caller") -> dict[str, PriceLayer]:
        caller.principal.require("settings.read")
        return await self.db.all(PriceLayer)

    async def set_price_layer(
        self, caller: "Caller", id: str | None, body: dict[str, Any]
    ) -> tuple[str, PriceLayer]:
        self._require(caller, "settings.write")
        from .operations import _validated

        layer = _validated(PriceLayer, body, "the price layer")
        existing = await self.db.all(PriceLayer)
        if id is None:
            id = slug(layer.role.replace(".", "-"), set(existing))
        elif not ID.match(id):
            raise AccountError("a layer's id is 1 to 64 letters, digits or . _ -")
        await self.db.put(layer, id)
        await self.audit.record(
            caller.principal.name,
            "setting.change",
            source=caller.source,
            details={"kind": "price.layer", "id": id},
        )
        return id, layer

    async def delete_price_layer(self, caller: "Caller", id: str) -> None:
        self._require(caller, "settings.write")
        if not await self.db.delete(PriceLayer, id):
            raise AccountError(f"no price layer {id!r}")
        vat = await self.db.get(Vat)
        if vat is not None and id in vat.applies_to:
            await self.db.put(
                Vat(rate=vat.rate, applies_to=tuple(a for a in vat.applies_to if a != id))
            )
        await self.audit.record(
            caller.principal.name,
            "setting.delete",
            source=caller.source,
            details={"kind": "price.layer", "id": id},
        )

    async def vat(self, caller: "Caller") -> Vat | None:
        caller.principal.require("settings.read")
        return await self.db.get(Vat)

    async def set_vat(self, caller: "Caller", body: dict[str, Any]) -> Vat:
        self._require(caller, "settings.write")
        from .operations import _validated

        vat = _validated(Vat, body, "VAT")
        await self.db.put(vat)
        await self.audit.record(
            caller.principal.name,
            "setting.change",
            source=caller.source,
            details={"kind": "price.vat"},
        )
        return vat

    def offered_series(self, caller: "Caller") -> list[dict[str, Any]]:
        """Every series the plugins offer, with whether it holds what it should."""
        caller.principal.require("points.read")
        if self.series is None:
            return []
        out = []
        for key, followed in sorted(
            self.series.followed.items(), key=lambda kv: (kv[0].instance, kv[0].series)
        ):
            freshness, why = self.series.freshness(key)
            info = followed.info
            out.append(
                {
                    "instance": key.instance,
                    "series": key.series,
                    "kind": info.kind,
                    "role": info.role,
                    "covers": list(info.covers.value or ()),
                    "unit": info.unit,
                    "vat": info.vat,
                    "resolution": info.resolution,
                    "area": info.area,
                    "freshness": freshness,
                    "why": why,
                    "known_until": followed.known_until,
                }
            )
        return out

    # --- the sources -------------------------------------------------------------------

    async def price_sources(self, caller: "Caller") -> dict[str, dict[str, Any]]:
        """The price plugins' instances, with their settings and how they are doing."""
        caller.principal.require("settings.read")
        out = {}
        for id, plugin in (await self.db.all(Plugin)).items():
            if plugin.plugin not in PRICE_PLUGINS:
                continue
            instance = self.host.instances.get(id) if self.host is not None else None
            health = instance.health if instance is not None else None
            out[id] = {
                "plugin": plugin.plugin,
                "settings": plugin.settings,
                "state": str(instance.state) if instance is not None else "stopped",
                "needs_user_action": health.needs_user_action if health else None,
                "error": instance.last_error if instance is not None else None,
            }
        return out

    async def set_price_source(
        self, caller: "Caller", plugin: str, id: str, settings: dict[str, Any]
    ) -> Plugin:
        """Add or change a price plugin's instance, and (re)start it."""
        self._require(caller, "plugins.manage", step_up=True)
        from .operations import _validated

        if plugin not in PRICE_PLUGINS:
            raise AccountError(f"{plugin!r} isn't a price source")
        if not ID.match(id):
            raise AccountError("an instance name is 1 to 64 letters, digits or . _ -")
        existing = await self.db.get(Plugin, id)
        if existing is not None and existing.plugin != plugin:
            raise AccountError(f"{id!r} is already a {existing.plugin} instance")
        setting = _validated(Plugin, {"plugin": plugin, "settings": settings}, "the price source")
        await self.db.put(setting, id)
        await self.audit.record(
            caller.principal.name,
            "plugin.change",
            source=caller.source,
            details={"instance": id, "plugin": plugin},
        )
        if self.host is not None:
            await self.host.apply(id)
        return setting

    async def set_fallbacks(self, caller: "Caller", id: str, fallbacks: list[str]) -> PriceLayer:
        """The series that stand in for a layer's own, in order."""
        self._require(caller, "settings.write")
        from .operations import _validated

        layer = await self.db.get(PriceLayer, id)
        if layer is None:
            raise AccountError(f"no price layer {id!r}")
        body = {**layer.model_dump(mode="json"), "fallbacks": [f for f in fallbacks if f]}
        changed = _validated(PriceLayer, body, "the price layer")
        await self.db.put(changed, id)
        await self.audit.record(
            caller.principal.name,
            "setting.change",
            source=caller.source,
            details={"kind": "price.layer", "id": id},
        )
        return changed

    # --- the stack ----------------------------------------------------------------------

    async def price_stack(self, caller: "Caller", day: date) -> Stack:
        caller.principal.require("points.read")
        if self.series is None:
            return Stack(None, [], [], [])
        zone = self.zone if isinstance(self.zone, ZoneInfo) else ZoneInfo("UTC")
        layers = await self.db.all(PriceLayer)
        return await assemble(layers, await self.db.get(Vat), self.series, day, zone)
