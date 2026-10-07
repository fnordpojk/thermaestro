"""The weather page: the providers and what each gives, the choice per quantity, the
forecast as used, how each provider has done at this house, and the location's climate.
Each form posts to an endpoint that does what its API counterpart does."""

import asyncio
from datetime import datetime, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, Response

from ..auth import AccountError
from ..cap.vocabulary import WEATHER
from ..core.weather import LEADS_H, SCORED
from ..open_meteo.plugin import MODELS
from .app import action, caller, services
from .operations import Caller, NeedsConfirmation
from .pages import Text, back, render
from .pages import _message as message
from .weather_operations import SHOWN

router = APIRouter(default_response_class=HTMLResponse)

Logged = Annotated[Caller, Depends(caller)]
STEP_H = 3
"""The forecast table's step."""
HA_WAIT_S = 3.0
"""How long the page waits for Home Assistant's list of weather entities."""


async def _weather(request: Request, who: Caller, status_code: int = 200, **extra: Any) -> Response:
    s = services(request)
    register = s.weather_register(who)
    forecast = await s.weather_forecast(who, 48)
    plugins = await s.plugins(who)
    ha = [id for id, p in plugins.items() if p.plugin == "homeassistant"]
    ha_weather: list[tuple[str, str, str]] = []
    for id in ha if who.principal.allows("plugins.manage") else ():
        try:
            async with asyncio.timeout(HA_WAIT_S):
                entities = await s.home_assistant_entities(who, id)
        except (AccountError, TimeoutError):
            continue  # the page doesn't wait for a Home Assistant that isn't answering
        ha_weather += [(id, e.entity_id, e.name) for e in entities if e.domain == "weather"]
    return render(
        request,
        "weather.html",
        who,
        status_code=status_code,
        location=await s.location(who),
        sources=await s.weather_sources(who),
        register=register,
        choice=await s.weather_choice(who),
        quantities=list(WEATHER),
        table=_table(forecast, s.zone),
        forecast={f["quantity"]: f for f in forecast},
        shown=SHOWN,
        any_derived=any(f["derived"] for f in forecast if f["quantity"] in SHOWN),
        scores=_scores(await s.weather_scores(who)),
        leads=LEADS_H,
        scored=SCORED,
        climate=await s.climate(who),
        has_open_meteo=any(p.plugin == "open_meteo" for p in plugins.values()),
        models=MODELS,
        ha_weather=ha_weather,
        **extra,
    )


def _table(forecast: list[dict[str, Any]], zone: Any) -> list[tuple[datetime, dict[str, Any]]]:
    """Every third hour for two days: each shown quantity's value from the interval
    holding that hour."""
    by_quantity = {f["quantity"]: f["values"] for f in forecast if f["quantity"] in SHOWN}
    starts = [datetime.fromisoformat(v["start"]) for values in by_quantity.values() for v in values]
    if not starts:
        return []
    first = max(min(starts), datetime.now(zone).replace(minute=0, second=0, microsecond=0))
    rows = []
    for n in range(0, 48, STEP_H):
        t = first + timedelta(hours=n)
        row = {}
        for quantity, values in by_quantity.items():
            for v in values:
                if datetime.fromisoformat(v["start"]) <= t < datetime.fromisoformat(v["end"]):
                    row[quantity] = v["value"]
                    break
        rows.append((t.astimezone(zone), row))
    return rows


def _scores(scores: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """A row per provider and quantity, with its scores by lead time."""
    rows: dict[tuple[str, str], dict[int, dict[str, Any]]] = {}
    for s in scores:
        rows.setdefault((s["source"], s["quantity"]), {})[s["lead_h"]] = s
    return [
        {"source": source, "quantity": quantity, "by_lead": by_lead}
        for (source, quantity), by_lead in sorted(rows.items())
    ]


@router.get("/weather")
async def weather_page(request: Request, who: Logged) -> Response:
    return await _weather(request, who)


async def _attempt(request: Request, who: Caller, work: Any, then: str = "/weather") -> Response:
    try:
        await work
    except NeedsConfirmation:
        raise
    except AccountError as e:
        return await _weather(request, who, 400, error=message(e))
    return back(then)


@router.post("/weather/sources")
@action("weather_source.write")
async def add_source(request: Request, who: Logged, plugin: Text, model: Text = "") -> Response:
    work = services(request).set_weather_source(who, plugin, None, model or None)
    return await _attempt(request, who, work, "/weather#providers")


@router.post("/weather/sources/{id}/delete")
@action("weather_source.delete")
async def delete_source(request: Request, who: Logged, id: str) -> Response:
    work = services(request).delete_weather_source(who, id)
    return await _attempt(request, who, work, "/weather#providers")


@router.post("/weather/homeassistant")
@action("weather_homeassistant.write")
async def add_home_assistant(request: Request, who: Logged, entity: Text) -> Response:
    id, _, name = entity.partition(" ")
    work = services(request).add_home_assistant_weather(who, id, name)
    return await _attempt(request, who, work, "/weather#providers")


@router.post("/weather/choice")
@action("weather_choice.write")
async def set_choice(
    request: Request,
    who: Logged,
    main: Text = "",
    fallback: Text = "",
    fallbacks: Annotated[list[str] | None, Form()] = None,
) -> Response:
    form = await request.form()
    quantities = {
        q: value for q in WEATHER if isinstance(value := form.get(f"q_{q}"), str) and value
    }
    body = {"main": main, "quantities": quantities, "fallbacks": [fallback, *(fallbacks or [])]}
    work = services(request).set_weather_choice(who, body)
    return await _attempt(request, who, work, "/weather#choice")


@router.post("/weather/climate")
@action("climate.write")
async def set_climate(
    request: Request, who: Logged, annual_mean: Text, monthly_spread: Text
) -> Response:
    try:
        body = {
            "annual_mean": float(annual_mean.replace(",", ".")),
            "monthly_spread": float(monthly_spread.replace(",", ".")),
        }
    except ValueError:
        return await _weather(request, who, 400, error=message(AccountError("not a number")))
    return await _attempt(
        request, who, services(request).set_climate(who, body), "/weather#climate"
    )


@router.post("/weather/climate/fetch")
@action("climate.fetch")
async def fetch_climate(request: Request, who: Logged) -> Response:
    return await _attempt(request, who, services(request).fetch_climate(who), "/weather#climate")
