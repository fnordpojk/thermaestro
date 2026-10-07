"""Prices: the layers of the stack, VAT, the series the plugins offer, and the stack
itself per day; the operations behind the prices page and its API."""

from datetime import date
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from ..auth import AccountError
from ..core.prices import Stack, assemble
from ..store import PriceLayer, Vat
from .sensor_operations import ID, slug

if TYPE_CHECKING:
    from datetime import tzinfo

    from ..core.audit import AuditLog
    from ..core.series import Series
    from ..store import Database
    from .operations import Caller


class PriceOperations:
    """Mixed into the services, whose parts it uses."""

    if TYPE_CHECKING:
        db: Database
        audit: AuditLog
        series: Series | None
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

    async def price_stack(self, caller: "Caller", day: date) -> Stack:
        caller.principal.require("points.read")
        if self.series is None:
            return Stack(None, [], [], [])
        zone = self.zone if isinstance(self.zone, ZoneInfo) else ZoneInfo("UTC")
        layers = await self.db.all(PriceLayer)
        return await assemble(layers, await self.db.get(Vat), self.series, day, zone)
