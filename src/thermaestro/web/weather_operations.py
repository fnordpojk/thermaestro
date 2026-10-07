"""Weather: the providers and what each gives (the register), the choice per quantity,
the forecast as used, how each provider has done at this house, and the location's
climate; the operations behind the weather page and its API.

A weather plugin forecasts for the location, so its settings are the location's
coordinates, kept in step when the location changes.
"""

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from ..auth import AccountError
from ..cap.vocabulary import WEATHER
from ..core.weather import DERIVABLE, ENOUGH
from ..open_meteo import archive
from ..seriesplugin import SourceError
from ..store import Climate, Location, Plugin, WeatherChoice
from .sensor_operations import HA_PLUGIN, ID, slug

if TYPE_CHECKING:
    from datetime import tzinfo

    from ..core.audit import AuditLog
    from ..core.host import PluginHost
    from ..core.series import Series
    from ..core.weather import Weather
    from ..store import Database
    from .operations import Caller

WEATHER_PLUGINS = ("met_norway", "smhi", "open_meteo")
SHOWN = (
    "temperature",
    "dew_point",
    "relative_humidity",
    "irradiance.global",
    "cloud_cover",
    "wind_speed",
    "precipitation",
)
"""The quantities the forecast table shows, in order."""


class WeatherOperations:
    """Mixed into the services, whose parts it uses."""

    archive_url: str = archive.URL

    if TYPE_CHECKING:
        db: Database
        audit: AuditLog
        series: Series | None
        host: PluginHost | None
        weather: Weather | None
        zone: tzinfo

        def _require(self, caller: Caller, permission: str, *, step_up: bool = False) -> None: ...

        async def _put_home_assistant(
            self, caller: Caller, id: str, settings: dict[str, Any]
        ) -> Plugin: ...

    # --- the providers ------------------------------------------------------------------

    async def weather_sources(self, caller: "Caller") -> dict[str, dict[str, Any]]:
        """The weather plugins' instances, with their settings and how they are doing."""
        caller.principal.require("settings.read")
        out = {}
        for id, plugin in (await self.db.all(Plugin)).items():
            if plugin.plugin not in WEATHER_PLUGINS:
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

    async def set_weather_source(
        self, caller: "Caller", plugin: str, id: str | None = None, model: str | None = None
    ) -> tuple[str, Plugin]:
        """Add a weather plugin's instance (no id: one is made from the plugin's name),
        or change its model, and (re)start it. It forecasts for the location."""
        self._require(caller, "plugins.manage", step_up=True)
        from .operations import _validated

        if plugin not in WEATHER_PLUGINS:
            raise AccountError(f"{plugin!r} isn't a weather provider")
        location = await self.db.get(Location)
        if location is None:
            raise AccountError("set the location first: forecasts are for a place")
        plugins = await self.db.all(Plugin)
        if id is None:
            id = slug(plugin.replace("_", "-"), set(plugins))
        elif not ID.match(id):
            raise AccountError("an instance name is 1 to 64 letters, digits or . _ -")
        existing = plugins.get(id)
        if existing is not None and existing.plugin != plugin:
            raise AccountError(f"{id!r} is already a {existing.plugin} instance")
        settings: dict[str, Any] = {"latitude": location.latitude, "longitude": location.longitude}
        if plugin == "open_meteo":
            settings["model"] = model or "best_match"
        setting = _validated(Plugin, {"plugin": plugin, "settings": settings}, "the provider")
        await self.db.put(setting, id)
        await self.audit.record(
            caller.principal.name,
            "plugin.change",
            source=caller.source,
            details={"instance": id, "plugin": plugin},
        )
        if self.host is not None:
            await self.host.apply(id)
        return id, setting

    async def delete_weather_source(self, caller: "Caller", id: str) -> None:
        """Stop and remove a weather plugin's instance, and take it out of the choice."""
        self._require(caller, "plugins.manage", step_up=True)
        plugin = await self.db.get(Plugin, id)
        if plugin is None or plugin.plugin not in WEATHER_PLUGINS:
            raise AccountError(f"no weather provider {id!r}")
        await self.db.delete(Plugin, id)
        choice = await self.db.get(WeatherChoice)
        if choice is not None:
            await self.db.put(_without(choice, id))
        await self.audit.record(
            caller.principal.name,
            "plugin.delete",
            source=caller.source,
            details={"instance": id, "plugin": plugin.plugin},
        )
        if self.host is not None:
            await self.host.apply(id)

    async def add_home_assistant_weather(self, caller: "Caller", id: str, entity: str) -> Plugin:
        """Read a Home Assistant weather entity's forecast, to stand in for the chosen
        provider."""
        plugin = await self.db.get(Plugin, id)
        if plugin is None or plugin.plugin != HA_PLUGIN:
            raise AccountError(f"no Home Assistant connection {id!r}")
        if not entity.startswith("weather."):
            raise AccountError(f"{entity!r} isn't a weather entity")
        listed = plugin.settings.get("entities")
        entities = [e for e in listed if isinstance(e, str)] if isinstance(listed, list) else []
        if entity not in entities:
            entities.append(entity)
        return await self._put_home_assistant(caller, id, {**plugin.settings, "entities": entities})

    async def _weather_follows_location(self, caller: "Caller", location: Location) -> None:
        """Move every weather plugin's instance to the new location."""
        for id, plugin in (await self.db.all(Plugin)).items():
            if plugin.plugin not in WEATHER_PLUGINS:
                continue
            settings = {
                **plugin.settings,
                "latitude": location.latitude,
                "longitude": location.longitude,
            }
            if settings == plugin.settings:
                continue
            await self.db.put(Plugin(plugin=plugin.plugin, settings=settings), id)
            await self.audit.record(
                caller.principal.name,
                "plugin.change",
                source=caller.source,
                details={"instance": id, "plugin": plugin.plugin, "why": "the location changed"},
            )
            if self.host is not None:
                await self.host.apply(id)

    # --- the register ------------------------------------------------------------------

    def weather_register(self, caller: "Caller") -> list[dict[str, Any]]:
        """Each provider, its terms, and for each quantity whether it gives it, derives it
        or lacks it, with its steps and horizon."""
        caller.principal.require("points.read")
        if self.weather is None or self.series is None:
            return []
        out = []
        for key, source in self.weather.sources().items():
            quantities = []
            for quantity in WEATHER:
                how = self.weather.has(source, quantity)
                info = source.infos.get(quantity)
                freshness, why = (
                    self.series.freshness(source.series(quantity)) if info else (None, None)
                )
                quantities.append(
                    {
                        "quantity": quantity,
                        "how": how or "missing",
                        "from": list(DERIVABLE.get(quantity, ())) if how == "derived" else [],
                        "unit": WEATHER[quantity],
                        "resolution": info.resolution if info else None,
                        "horizon": info.horizon if info else None,
                        "steps": [s.model_dump(mode="json") for s in info.steps] if info else [],
                        "freshness": freshness,
                        "why": why,
                    }
                )
            first = next(iter(source.infos.values()))
            out.append(
                {
                    "key": key,
                    "label": source.label,
                    "instance": source.instance,
                    "entity": source.entity,
                    "provider": source.provider.model_dump(mode="json")
                    if source.provider
                    else None,
                    "models": first.models.model_dump(mode="json"),
                    "updates": first.updates.model_dump(mode="json"),
                    "quantities": quantities,
                }
            )
        return out

    # --- the choice ----------------------------------------------------------------------

    async def weather_choice(self, caller: "Caller") -> WeatherChoice:
        caller.principal.require("settings.read")
        return await self.db.get(WeatherChoice) or WeatherChoice()

    async def set_weather_choice(self, caller: "Caller", body: dict[str, Any]) -> WeatherChoice:
        """The main provider, one per quantity where another is wanted, and the
        fallbacks, in order."""
        self._require(caller, "settings.write")
        from .operations import _validated

        cleaned = {
            "main": body.get("main") or None,
            "quantities": {q: s for q, s in (body.get("quantities") or {}).items() if s},
            "fallbacks": [f for f in body.get("fallbacks") or [] if f],
        }
        choice = _validated(WeatherChoice, cleaned, "the weather choice")
        unknown = set(choice.quantities) - set(WEATHER)
        if unknown:
            raise AccountError(f"no forecast quantity {sorted(unknown)[0]!r}")
        instances = await self.db.all(Plugin)
        for key in (choice.main, *choice.quantities.values(), *choice.fallbacks):
            if key is not None and key.split(":", 1)[0] not in instances:
                raise AccountError(f"no weather provider {key!r}")
        await self.db.put(choice)
        await self.audit.record(
            caller.principal.name,
            "setting.change",
            source=caller.source,
            details={"kind": "weather"},
        )
        return choice

    # --- the forecast and the scores ------------------------------------------------------

    async def weather_forecast(self, caller: "Caller", hours: int = 48) -> list[dict[str, Any]]:
        """The forecast as used, per quantity: where it comes from, and its values."""
        caller.principal.require("points.read")
        if self.weather is None:
            return []
        start = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
        end = start + timedelta(hours=hours)
        out = []
        for quantity in WEATHER:
            found = await self.weather.forecast(quantity, start, end)
            out.append(
                {
                    "quantity": quantity,
                    "unit": WEATHER[quantity],
                    "source": found.source,
                    "derived": found.derived,
                    "fallback": found.fallback,
                    "stale": found.stale,
                    "values": [
                        {"start": s.start.isoformat(), "end": s.end.isoformat(), "value": s.value}
                        for s in found.spans
                        if s.end > start
                    ],
                }
            )
        return out

    async def weather_scores(self, caller: "Caller") -> list[dict[str, Any]]:
        """Bias and mean absolute error per provider, quantity and lead time, over the
        last 30 days; `enough` says whether there are enough comparisons to go by."""
        caller.principal.require("points.read")
        if self.weather is None:
            return []
        return [
            {
                "source": s.source,
                "quantity": s.quantity,
                "lead_h": s.lead_h,
                "n": s.n,
                "bias": s.bias,
                "mae": s.mae,
                "enough": s.n >= ENOUGH,
            }
            for s in await self.weather.scores()
        ]

    # --- the climate ---------------------------------------------------------------------

    async def climate(self, caller: "Caller") -> Climate | None:
        caller.principal.require("settings.read")
        return await self.db.get(Climate)

    async def set_climate(self, caller: "Caller", body: dict[str, Any]) -> Climate:
        """The annual mean and the monthly spread, as the user knows them."""
        self._require(caller, "settings.write")
        from .operations import _validated

        climate = _validated(
            Climate,
            {
                "annual_mean": body.get("annual_mean"),
                "monthly_spread": body.get("monthly_spread"),
                "source": "user",
            },
            "the climate",
        )
        await self._put_climate(caller, climate)
        return climate

    async def fetch_climate(self, caller: "Caller") -> Climate:
        """The climate from Open-Meteo's archive: the last ten whole years at the
        location. Asked only by an installation that uses Open-Meteo."""
        self._require(caller, "settings.write")
        if not any(p.plugin == "open_meteo" for p in (await self.db.all(Plugin)).values()):
            raise AccountError("add Open-Meteo as a provider first, or enter the climate yourself")
        location = await self.db.get(Location)
        if location is None:
            raise AccountError("set the location first: the climate is for a place")
        try:
            normals = await archive.normals(
                location.latitude,
                location.longitude,
                datetime.now(self.zone).date(),
                url=self.archive_url,
            )
        except SourceError as e:
            raise AccountError(str(e)) from None
        climate = Climate(
            annual_mean=normals.annual_mean,
            monthly_spread=normals.monthly_spread,
            monthly_means=normals.monthly_means,
            source="open_meteo",
            period=normals.period,
        )
        await self._put_climate(caller, climate)
        return climate

    async def _put_climate(self, caller: "Caller", climate: Climate) -> None:
        await self.db.put(climate)
        await self.audit.record(
            caller.principal.name,
            "setting.change",
            source=caller.source,
            details={"kind": "climate", "source": climate.source},
        )


def _without(choice: WeatherChoice, instance: str) -> WeatherChoice:
    def keep(key: str | None) -> bool:
        return key is not None and key.split(":", 1)[0] != instance

    return WeatherChoice(
        main=choice.main if keep(choice.main) else None,
        quantities={q: s for q, s in choice.quantities.items() if keep(s)},
        fallbacks=tuple(f for f in choice.fallbacks if keep(f)),
    )
