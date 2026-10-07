"""The API for weather: the providers and what each gives, the choice per quantity, the
forecast as used, the scores at this house, and the location's climate."""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request

from ..auth import AccountError
from .app import action, caller, services
from .operations import Caller

router = APIRouter(prefix="/api/v1")

Logged = Annotated[Caller, Depends(caller)]


@router.get("/weather")
@action("weather.read")
async def register(request: Request, who: Logged) -> dict[str, Any]:
    """Each provider with its terms and, per quantity, whether it gives it, derives it or
    lacks it; and the choice."""
    s = services(request)
    choice = await s.weather_choice(who)
    return {"providers": s.weather_register(who), "choice": choice.model_dump(mode="json")}


@router.put("/weather/choice")
@action("weather_choice.write")
async def set_choice(body: dict[str, Any], request: Request, who: Logged) -> dict[str, Any]:
    """`{"main": "met", "quantities": {"irradiance.global": "open-meteo"}, "fallbacks":
    ["homeassistant:weather.forecast_home"]}`."""
    return (await services(request).set_weather_choice(who, body)).model_dump(mode="json")


@router.get("/weather/forecast")
@action("weather_forecast.read")
async def forecast(
    request: Request, who: Logged, hours: Annotated[int, Query(ge=1, le=240)] = 48
) -> list[dict[str, Any]]:
    """The forecast as used, per quantity: its provider, whether derived or a fallback,
    and its values from this hour on."""
    return await services(request).weather_forecast(who, hours)


@router.get("/weather/scores")
@action("weather_scores.read")
async def scores(request: Request, who: Logged) -> list[dict[str, Any]]:
    """How each provider has done at this house, by lead time, over the last 30 days."""
    return await services(request).weather_scores(who)


@router.get("/weather/sources")
@action("weather_sources.read")
async def sources(request: Request, who: Logged) -> dict[str, Any]:
    return await services(request).weather_sources(who)


@router.put("/weather/sources/{id}")
@action("weather_source.write")
async def set_source(
    id: str, body: dict[str, Any], request: Request, who: Logged
) -> dict[str, Any]:
    """Add or change a provider: `{"plugin": "met_norway"}`, or `{"plugin": "open_meteo",
    "model": "icon_seamless"}`. It forecasts for the location."""
    plugin, model = body.get("plugin"), body.get("model")
    if not isinstance(plugin, str) or not isinstance(model, str | None):
        raise AccountError("give the plugin, and for Open-Meteo maybe a model")
    _, setting = await services(request).set_weather_source(who, plugin, id, model)
    return setting.model_dump(mode="json")


@router.delete("/weather/sources/{id}")
@action("weather_source.delete")
async def delete_source(id: str, request: Request, who: Logged) -> dict[str, str]:
    await services(request).delete_weather_source(who, id)
    return {"status": "ok"}


@router.put("/weather/homeassistant/{id}")
@action("weather_homeassistant.write")
async def add_home_assistant(
    id: str, body: dict[str, Any], request: Request, who: Logged
) -> dict[str, Any]:
    """Read a Home Assistant weather entity's forecast: `{"entity": "weather.forecast_home"}`."""
    entity = body.get("entity")
    if not isinstance(entity, str):
        raise AccountError("give the weather entity")
    setting = await services(request).add_home_assistant_weather(who, id, entity)
    return setting.model_dump(mode="json")


@router.get("/weather/climate")
@action("climate.read")
async def climate(request: Request, who: Logged) -> dict[str, Any] | None:
    found = await services(request).climate(who)
    return found.model_dump(mode="json") if found else None


@router.put("/weather/climate")
@action("climate.write")
async def set_climate(body: dict[str, Any], request: Request, who: Logged) -> dict[str, Any]:
    """`{"annual_mean": 7.5, "monthly_spread": 20.0}`, in °C and K."""
    return (await services(request).set_climate(who, body)).model_dump(mode="json")


@router.post("/weather/climate/fetch")
@action("climate.fetch")
async def fetch_climate(request: Request, who: Logged) -> dict[str, Any]:
    """The climate from Open-Meteo's archive: the last ten whole years at the location."""
    return (await services(request).fetch_climate(who)).model_dump(mode="json")
