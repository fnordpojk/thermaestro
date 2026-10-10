"""Prices: the layers of the stack, VAT, the series the plugins offer, and the stack
itself per day; the operations behind the prices page and its API."""

from datetime import date
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from pydantic import BaseModel

from ..auth import AccountError
from ..core.gridrules import household
from ..core.prices import Stack, assemble
from ..spotsources import choices
from ..store import EntsoE, GridRule, OctopusAgile, Plugin, PriceLayer, SpotZone, Tibber, Vat
from ..zones import ZONES
from .sensor_operations import ID, slug

if TYPE_CHECKING:
    from datetime import tzinfo

    from ..core.audit import AuditLog
    from ..core.host import PluginHost
    from ..core.series import Series
    from ..store import Database
    from .operations import Caller

PRICE_PLUGINS = ("tibber", "entsoe", "energy_charts", "nordic_sites", "omie", "octopus_agile")
NO_ACCOUNT = ("energy_charts", "nordic_sites", "omie")
"""Spot price sources set up by the bidding zone alone, and removed when no longer used."""


def _source_settings(plugin: str, settings: dict[str, Any]) -> None:
    """Refuse settings the price plugin would refuse when it starts."""
    from ..nordic_sites.plugin import SITES
    from ..omie.plugin import COLUMN
    from .operations import _validated

    models: dict[str, type[BaseModel]] = {
        "tibber": Tibber,
        "entsoe": EntsoE,
        "energy_charts": SpotZone,
        "nordic_sites": SpotZone,
        "omie": SpotZone,
        "octopus_agile": OctopusAgile,
    }
    _validated(models[plugin], settings, "the price source")
    zone = settings.get("zone")
    if zone is None:
        return
    area = ZONES.get(str(zone))
    if (
        area is None
        or (plugin == "nordic_sites" and area.country not in SITES)
        or (plugin == "omie" and zone not in COLUMN)
    ):
        raise AccountError(f"{plugin} has no prices for {zone!r}")


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

    # --- grid rules -----------------------------------------------------------------------

    async def grid_rules(self, caller: "Caller") -> dict[str, GridRule]:
        caller.principal.require("settings.read")
        return await self.db.all(GridRule)

    async def set_grid_rule(
        self, caller: "Caller", id: str | None, body: dict[str, Any]
    ) -> tuple[str, GridRule]:
        """Keep a grid rule. A time-of-use rule is a layer of the stack too, made with it:
        its role `grid.tou`, its unit and VAT the rule's."""
        self._require(caller, "settings.write")
        from .operations import _validated

        rule = _validated(GridRule, body, "the grid rule")
        existing = await self.db.all(GridRule)
        if id is None:
            id = slug(rule.owner, set(existing))
        elif not ID.match(id):
            raise AccountError("a rule's id is 1 to 64 letters, digits or . _ -")
        await self.db.put(rule, id)
        layers = await self.db.all(PriceLayer)
        layer = next(
            (lid for lid, x in layers.items() if x.source == "rule" and x.rule == id), None
        )
        if rule.type == "tou":
            made = PriceLayer(
                role="grid.tou", source="rule", rule=id, unit=rule.unit or "", vat=rule.vat
            )
            await self.db.put(made, layer or slug(f"grid-{id}", set(layers)))
        elif layer is not None:
            await self._drop_layer(layer)
        await self.audit.record(
            caller.principal.name,
            "setting.change",
            source=caller.source,
            details={"kind": "grid.rule", "id": id},
        )
        return id, rule

    async def delete_grid_rule(self, caller: "Caller", id: str) -> None:
        """Remove a grid rule, and the layer that holds its prices."""
        self._require(caller, "settings.write")
        if not await self.db.delete(GridRule, id):
            raise AccountError(f"no grid rule {id!r}")
        for lid, layer in (await self.db.all(PriceLayer)).items():
            if layer.source == "rule" and layer.rule == id:
                await self._drop_layer(lid)
        await self.audit.record(
            caller.principal.name,
            "setting.delete",
            source=caller.source,
            details={"kind": "grid.rule", "id": id},
        )

    async def _drop_layer(self, id: str) -> None:
        await self.db.delete(PriceLayer, id)
        vat = await self.db.get(Vat)
        if vat is not None and id in vat.applies_to:
            await self.db.put(
                Vat(rate=vat.rate, applies_to=tuple(a for a in vat.applies_to if a != id))
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
        followed = (
            sorted(self.series.followed.items(), key=lambda kv: (kv[0].instance, kv[0].series))
            if self.series is not None
            else []
        )
        for id, plugin in (await self.db.all(Plugin)).items():
            if plugin.plugin not in PRICE_PLUGINS:
                continue
            instance = self.host.instances.get(id) if self.host is not None else None
            health = instance.health if instance is not None else None
            # The area and currency as the source reports them, set or learned (Tibber's).
            reported = [f.info for k, f in followed if k.instance == id and f.info.kind == "price"]
            areas = [info.area for info in reported if info.area]
            out[id] = {
                "plugin": plugin.plugin,
                "settings": plugin.settings,
                "state": str(instance.state) if instance is not None else "stopped",
                "needs_user_action": health.needs_user_action if health else None,
                "error": instance.last_error if instance is not None else None,
                "area": areas[0] if areas else None,
                "currency": reported[0].unit.split("/")[0] if reported else None,
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
        _source_settings(plugin, settings)
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

    async def choose_spot(
        self,
        caller: "Caller",
        zone: str,
        source: str,
        fallback: str | None = None,
        currency: str | None = None,
    ) -> tuple[str, PriceLayer]:
        """Take a bidding zone's spot price from `source`, with `fallback` standing in on a
        day it has no price, then ENTSO-E where a token is entered, then every other spot
        price on offer in the same unit (Tibber's, for a Tibber customer). Both are
        plugin names, from those `spotsources.choices` offers for the zone. Sources that
        need no account are set up for the zone, and removed once no longer used; one with
        a token keeps it. The stack's spot layer then takes from them, VAT charged on it
        where a VAT rule is set; its id and setting are returned. A supplier's total in the
        stack is split by the stack itself (`core.prices.splits`)."""
        self._require(caller, "plugins.manage", step_up=True)
        area = ZONES.get(zone)
        if area is None:
            raise AccountError(f"no bidding zone {zone!r}")
        chosen_currency = (currency or "").strip().upper() or area.currency
        if len(chosen_currency) != 3 or not chosen_currency.isalpha():
            raise AccountError("a currency is three letters, such as EUR")
        sources = await self.price_sources(caller)
        has = {p["plugin"]: id for id, p in sorted(sources.items(), reverse=True)}
        offered = {
            c.plugin: c for c in choices(zone, tibber="tibber" in has, entsoe="entsoe" in has)
        }
        if source not in offered:
            raise AccountError(f"{source!r} isn't a source of {zone}'s prices")
        if fallback and (fallback not in offered or fallback == source):
            raise AccountError(f"{fallback!r} can't stand in for {source!r} in {zone}")
        used = [source, *([fallback] if fallback else [])]
        if "entsoe" in has and "entsoe" not in used:
            used.append("entsoe")
        settings = {"zone": zone, "currency": chosen_currency}
        refs = []
        for plugin in used:
            if plugin in NO_ACCOUNT:
                id = has.get(plugin, plugin)
                if sources.get(id, {}).get("settings") != settings:
                    await self.set_price_source(caller, plugin, id, settings)
            elif plugin == "entsoe":
                id = has[plugin]
                current = dict(sources[id]["settings"])
                if {k: current.get(k) for k in settings} != settings:
                    await self.set_price_source(caller, plugin, id, {**current, **settings})
            else:
                id = has[plugin]
            refs.append(f"{id}:{offered[plugin].series}")
        for id, p in sources.items():
            if p["plugin"] in NO_ACCOUNT and p["plugin"] not in used:
                await self._remove_price_source(caller, id, p["plugin"])
        offered_now = self.offered_series(caller)
        unit = next(
            (o["unit"] for o in offered_now if f"{o['instance']}:{o['series']}" == refs[0]),
            f"{chosen_currency}/kWh",
        )
        # Every other spot price on offer in the same unit stands in after the chosen ones.
        refs += [
            ref
            for o in offered_now
            if o["role"] == "energy.spot"
            and not o["covers"]
            and (o["unit"], o["vat"]) == (unit, "excl")
            and (ref := f"{o['instance']}:{o['series']}") not in refs
            and o["instance"] in sources
            and sources[o["instance"]]["plugin"] not in NO_ACCOUNT
        ]
        instance, _, series = refs[0].partition(":")
        layers = await self.db.all(PriceLayer)
        current_id = next((id for id, layer in layers.items() if layer.role == "energy.spot"), None)
        body = {
            "role": "energy.spot",
            "source": "series",
            "plugin": instance,
            "series": series,
            "fallbacks": refs[1:],
            "unit": unit,
            "vat": "excl",
        }
        spot_id, spot_layer = await self.set_price_layer(caller, current_id, body)
        await self._charge_vat_on(caller, [spot_id])
        return spot_id, spot_layer

    async def _charge_vat_on(self, caller: "Caller", ids: list[str]) -> None:
        """Have the VAT rule charge VAT on these layers too, where a rule is set."""
        vat = await self.db.get(Vat)
        layers = await self.db.all(PriceLayer)
        if vat is None:
            return
        added = [
            id for id in ids if id not in vat.applies_to and layers[id].role not in vat.applies_to
        ]
        if added:
            body = {"rate": vat.rate, "applies_to": [*vat.applies_to, *added]}
            await self.set_vat(caller, body)

    async def spot_choice(self, caller: "Caller") -> dict[str, str | None]:
        """Where the stack's spot layer takes its price from, as plugin names, and the
        bidding zone they are set up for. With no spot layer, the zone the sources are
        set up for, if any."""
        caller.principal.require("settings.read")
        sources = await self.price_sources(caller)
        layers = await self.db.all(PriceLayer)
        layer = next(
            (x for x in layers.values() if x.role == "energy.spot" and x.source == "series"), None
        )
        if layer is None:
            zoned = [
                s["settings"]["zone"] for _, s in sorted(sources.items()) if "zone" in s["settings"]
            ]
            return {"zone": zoned[0] if zoned else None, "source": None, "fallback": None}
        ids = [layer.plugin or "", *(ref.partition(":")[0] for ref in layer.fallbacks)]
        known = [sources[i] for i in ids if i in sources]
        zones = [s["settings"].get("zone") for s in known if s["settings"].get("zone")]
        plugins = [sources[i]["plugin"] if i in sources else None for i in ids]
        return {
            "zone": zones[0] if zones else None,
            "source": plugins[0],
            "fallback": plugins[1] if len(plugins) > 1 else None,
        }

    async def _remove_price_source(self, caller: "Caller", id: str, plugin: str) -> None:
        await self.db.delete(Plugin, id)
        await self.audit.record(
            caller.principal.name,
            "plugin.delete",
            source=caller.source,
            details={"instance": id, "plugin": plugin},
        )
        if self.host is not None:
            await self.host.apply(id)

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
        rules, holiday = await household(self.db, zone)
        vat = await self.db.get(Vat)
        return await assemble(layers, vat, self.series, day, zone, rules, holiday)
