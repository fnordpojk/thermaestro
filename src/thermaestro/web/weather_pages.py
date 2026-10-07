"""The weather page: the forecast as used, and how each provider has done at this house.
The providers and the choice per quantity are set under Setup → Weather, the location's
climate under Setup → House; their forms post here, each to an endpoint that does what its
API counterpart does."""

from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, Response

from ..auth import AccountError
from ..cap.vocabulary import WEATHER
from ..core import sun
from ..core.weather import LEADS_H, SCORED
from . import i18n, labels
from .app import action, caller, services
from .operations import Caller
from .pages import Text, render
from .pages import _message as message
from .setup_pages import attempt, show
from .weather_operations import SHOWN

router = APIRouter(default_response_class=HTMLResponse)

Logged = Annotated[Caller, Depends(caller)]
STEP_H = 3
"""The forecast table's step."""


@router.get("/weather")
async def weather_page(request: Request, who: Logged) -> Response:
    s = services(request)
    forecast = await s.weather_forecast(who, 48)
    location = await s.location(who)
    register = s.weather_register(who)
    return render(
        request,
        "weather.html",
        who,
        location=location,
        register=register,
        table=_table(forecast, s.zone),
        forecast={f["quantity"]: f for f in forecast},
        shown=SHOWN,
        any_derived=any(f["derived"] for f in forecast if f["quantity"] in SHOWN),
        scores=_scores(await s.weather_scores(who)),
        leads=LEADS_H,
        scored=SCORED,
        meteogram=_meteogram(forecast, register, location),
        decimal=i18n.decimal_symbol(),
        zone=i18n.zone_name(),
        formats=i18n.formats.get(),
    )


GRAPHED = (
    "temperature",
    "temperature.p10",
    "temperature.p90",
    "dew_point",
    "precipitation",
    "cloud_cover",
    "wind_speed",
    "wind_gust",
    "wind_direction",
    "irradiance.global",
)
"""What the weather graph draws, after Yr's meteogram."""


def _meteogram(
    forecast: list[dict[str, Any]], register: list[dict[str, Any]], location: Any
) -> dict[str, Any]:
    """The forecast for the graph: per quantity its values as [start, end, value] in
    seconds, and where it comes from; and for each hour whether the sun is up."""
    names = {p["key"]: p["label"] for p in register}
    series = {}
    for f in forecast:
        if f["quantity"] not in GRAPHED or not f["values"]:
            continue
        series[f["quantity"]] = {
            "name": labels.quantity(f["quantity"]),
            "unit": f["unit"],
            "source": names.get(f["source"], f["source"]),
            "derived": f["derived"],
            "fallback": f["fallback"],
            "values": [
                [
                    datetime.fromisoformat(v["start"]).timestamp(),
                    datetime.fromisoformat(v["end"]).timestamp(),
                    v["value"],
                ]
                for v in f["values"]
            ],
        }
    start = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
    daylight = []
    if location is not None:
        for n in range(49):
            middle = start + timedelta(hours=n, minutes=30)
            up = sun.cos_zenith(middle, location.latitude, location.longitude) > 0
            daylight.append([middle.timestamp() - 1800, up])
    return {
        "series": series,
        "daylight": daylight,
        "texts": {
            "derived": i18n._("derived"),
            "fallback": i18n._("fallback"),
            "per_hour": i18n._("mm per hour"),
            "empty": i18n._("No forecast yet: choose a main provider under Setup."),
            "sky": i18n._("Sky: drawn from the cloud cover, precipitation and the sun's height."),
        },
    }


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


@router.post("/weather/sources")
@action("weather_source.write")
async def add_source(request: Request, who: Logged, plugin: Text, model: Text = "") -> Response:
    work = services(request).set_weather_source(who, plugin, None, model or None)
    return await attempt(request, who, work, "weather", "/setup/weather#providers")


@router.post("/weather/sources/{id}/delete")
@action("weather_source.delete")
async def delete_source(request: Request, who: Logged, id: str) -> Response:
    work = services(request).delete_weather_source(who, id)
    return await attempt(request, who, work, "weather", "/setup/weather#providers")


@router.post("/weather/homeassistant")
@action("weather_homeassistant.write")
async def add_home_assistant(request: Request, who: Logged, entity: Text) -> Response:
    id, _, name = entity.partition(" ")
    work = services(request).add_home_assistant_weather(who, id, name)
    return await attempt(request, who, work, "weather", "/setup/weather#providers")


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
    return await attempt(request, who, work, "weather", "/setup/weather#choice")


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
        return await show(request, who, "house", 400, error=message(AccountError("not a number")))
    return await attempt(
        request, who, services(request).set_climate(who, body), "house", "/setup/house#climate"
    )


@router.post("/weather/climate/fetch")
@action("climate.fetch")
async def fetch_climate(request: Request, who: Logged) -> Response:
    return await attempt(
        request, who, services(request).fetch_climate(who), "house", "/setup/house#climate"
    )
