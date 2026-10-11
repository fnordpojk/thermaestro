"""Setup: a page per topic, holding everything the topic needs: its connections and their
state, its options, and what it offers. The forms post to the endpoints in the other page
modules; each shows its topic again on a refusal, and goes back to it when done."""

import asyncio
from collections.abc import Awaitable
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, Response

from ..auth import AccountError
from ..cap.vocabulary import WEATHER
from . import labels
from .app import action, caller, services
from .i18n import mark
from .operations import Caller, NeedsConfirmation
from .pages import _message, back, render, time_zones

router = APIRouter(default_response_class=HTMLResponse)

Logged = Annotated[Caller, Depends(caller)]

TOPICS = {
    "house": mark("House"),
    "pump": mark("Pump"),
    "external": mark("External"),
    "sensors": mark("Sensors"),
    "prices": mark("Prices"),
    "weather": mark("Weather"),
    "control": mark("Control"),
    "nibepi": mark("From NibePi"),
}

FIXED_ROLES = (
    "energy.supplier",
    "tax.energy",
    "grid.transfer",
    "grid.tou",
    "levy",
    "subsidy",
    "energy.spot",
)
OUTDOOR_QUANTITIES = ("temperature", "humidity", "atmospheric_pressure", "irradiance", "pm25")
HA_PLUGIN = "homeassistant"
HA_WAIT_S = 3.0
"""How long the weather topic waits for Home Assistant's list of weather entities."""


async def _house(request: Request, who: Caller) -> dict[str, Any]:
    s = services(request)
    plugins = await s.plugins(who)
    systems = []
    for instance_id, instance in sorted((s.host.instances if s.host else {}).items()):
        described = instance.described
        for node in described.nodes if described else ():
            if node.kind == "climate_system":
                systems.append(
                    (f"{instance_id}:{node.path}", s.node_label(who, instance_id, node.path))
                )
    rooms = await s.room_settings(who)
    areas: dict[str, list[str]] = {}
    for id, p in plugins.items() if who.principal.allows("settings.write") else ():
        if p.plugin != HA_PLUGIN:
            continue
        try:
            async with asyncio.timeout(HA_WAIT_S):
                areas[id] = await s.home_assistant_areas(who, id)
        except (AccountError, TimeoutError):
            continue  # the page doesn't wait for a Home Assistant that isn't answering
    return {
        "home": await s.home_settings(who),
        "areas": {id: names for id, names in areas.items() if names},
        "location": await s.location(who),
        "zones": time_zones(),
        "climate": await s.climate(who),
        "has_open_meteo": any(p.plugin == "open_meteo" for p in plugins.values()),
        "rooms": dict(sorted(rooms.items(), key=lambda kv: kv[1].name.lower())),
        "sensors": await s.sensor_settings(who),
        "systems": systems,
    }


async def _pump(request: Request, who: Caller) -> dict[str, Any]:
    from ..nibe.maps import load

    s = services(request)
    plugins = await s.plugins(who)
    pumps = {id: p for id, p in plugins.items() if p.plugin == "nibe"}
    states = {i["id"]: i for i in s.status(who) if i["id"] in pumps}
    return {
        "pumps": pumps,
        "states": states,
        "models": sorted(load("bus").models),
        "s_models": sorted(load("s-series").models),
    }


async def _external(request: Request, who: Caller) -> dict[str, Any]:
    s = services(request)
    plugins = await s.plugins(who)
    return {
        "connections": {id: p for id, p in plugins.items() if p.plugin == HA_PLUGIN},
        "states": {i["id"]: i for i in s.status(who)},
        "mqtt": await s.mqtt_settings(who),
        "mqtt_state": s.mqtt_state(who),
        "discovery": await s.discovery_settings(who),
        "discovery_state": s.discovery_state(who),
    }


async def _sensors(request: Request, who: Caller) -> dict[str, Any]:
    from .sensor_pages import SENSOR_QUANTITIES

    s = services(request)
    sensors = await s.sensor_settings(who)
    plugins = await s.plugins(who)
    return {
        "sensors": dict(sorted(sensors.items(), key=lambda kv: kv[1].name.lower())),
        "readings": s.sensor_readings(who),
        "rooms": await s.room_settings(who),
        "outdoor": (await s.outdoor(who)).references,
        "outdoor_quantities": OUTDOOR_QUANTITIES,
        "quantities": SENSOR_QUANTITIES,
        "connections": {id: p for id, p in plugins.items() if p.plugin == HA_PLUGIN},
        "has_broker": await s.mqtt_settings(who) is not None,
    }


async def _prices(request: Request, who: Caller) -> dict[str, Any]:
    from ..core.prices import splits
    from ..nordic_sites.plugin import SITES
    from ..octopus_agile.plugin import REGIONS
    from ..spotsources import TIBBER_COUNTRIES, choices
    from ..zones import ZONES

    s = services(request)
    sources = await s.price_sources(who)
    has = {p["plugin"] for p in sources.values()}
    spot = await s.spot_choice(who)
    asked = request.query_params.get("zone")
    zone = asked if asked in ZONES else spot["zone"]
    country = ZONES[zone].country if zone else None
    offered = choices(zone, tibber="tibber" in has, entsoe="entsoe" in has) if zone else []
    plugins = [c.plugin for c in offered]
    same = zone == spot["zone"]
    source = spot["source"] if same and spot["source"] in plugins else None
    fallback = spot["fallback"] if same and spot["fallback"] in plugins else None
    if source is None and plugins:
        source, fallback = plugins[0], plugins[1] if len(plugins) > 1 else None
    layers = await s.price_layers(who)
    names = {
        "energy_charts": "Energy-Charts",
        "entsoe": "ENTSO-E",
        "omie": "OMIE",
        "tibber": "Tibber",
        "nordic_sites": SITES[country].host.removeprefix("www.") if country in SITES else "",
    }
    return {
        "sources": sources,
        "tibber": {id: p for id, p in sources.items() if p["plugin"] == "tibber"},
        "entsoe": {id: p for id, p in sources.items() if p["plugin"] == "entsoe"},
        "octopus": {id: p for id, p in sources.items() if p["plugin"] == "octopus_agile"},
        "spot_zone": zone,
        # The sources only once asked for; after saving, the zone is chosen again.
        "spot_asked": asked in ZONES,
        "spot_choices": offered,
        "spot_source": source,
        "spot_fallback": fallback,
        "source_names": names,
        "split": splits(layers, s.series) if s.series is not None else {},
        "tibber_hint": country in TIBBER_COUNTRIES and "tibber" not in has,
        "octopus_regions": REGIONS,
        "bidding_zones": ZONES,
        "offered": s.offered_series(who),
        "layers": layers,
        "vat": await s.vat(who),
        "fixed_roles": FIXED_ROLES,
        "grid_rules": await s.grid_rules(who),
        "danish": {id: p for id, p in sources.items() if p["plugin"] == "energidataservice"},
        **(await _danish_companies(request, who)),
        "grid_layers": {x.rule: id for id, x in layers.items() if x.source == "rule"},
    }


async def _danish_companies(request: Request, who: Caller) -> dict[str, Any]:
    """Denmark's grid companies, fetched only when asked for on the page."""
    if request.query_params.get("denmark") != "1":
        return {"dk_companies": None, "dk_error": None}
    try:
        return {
            "dk_companies": await services(request).danish_grid_companies(who),
            "dk_error": None,
        }
    except AccountError as e:
        return {"dk_companies": None, "dk_error": str(e)}


async def _weather(request: Request, who: Caller) -> dict[str, Any]:
    from ..open_meteo.plugin import MODELS

    s = services(request)
    plugins = await s.plugins(who)
    ha_weather: list[tuple[str, str, str]] = []
    for id, p in plugins.items() if who.principal.allows("plugins.manage") else ():
        if p.plugin != HA_PLUGIN:
            continue
        try:
            async with asyncio.timeout(HA_WAIT_S):
                entities = await s.home_assistant_entities(who, id)
        except (AccountError, TimeoutError):
            continue  # the page doesn't wait for a Home Assistant that isn't answering
        ha_weather += [(id, e.entity_id, e.name) for e in entities if e.domain == "weather"]
    forecast = await s.weather_forecast(who, 48)
    return {
        "location": await s.location(who),
        "sources": await s.weather_sources(who),
        "register": s.weather_register(who),
        "choice": await s.weather_choice(who),
        "quantities": list(WEATHER),
        "forecast": {f["quantity"]: f for f in forecast},
        "models": MODELS,
        "ha_weather": ha_weather,
    }


async def _control(request: Request, who: Caller) -> dict[str, Any]:
    from ..core.executor import split

    s = services(request)
    rows, budgets = [], {}
    for lever in await s.levers(who):
        instance, path = split(lever["lever"])
        node = path.rpartition("/")[0]
        rows.append({**lever, "part": s.node_label(who, instance, node)})
        budgets[s.node_label(who, instance, path.split("/")[0])] = (
            lever["writes_today"],
            lever["budget"],
        )
    plugins = await s.plugins(who)
    open_ports = [
        (id, p.settings.get("host"), p.settings.get("write_port", 10000))
        for id, p in plugins.items()
        if p.plugin == "nibe" and p.settings.get("protocol", "nibegw") == "nibegw"
    ]
    return {
        "levers": rows,
        "budgets": budgets,
        "notices": s.plan(who)["notices"] if who.principal.allows("plan.read") else [],
        "open_ports": open_ports,
        "modes": labels.MODES,
    }


async def _nibepi(request: Request, who: Caller) -> dict[str, Any]:
    """Nothing until a file is read; then its draft, by the token in the query. And the
    review of the pump's settings."""
    s = services(request)
    empty: dict[str, Any] = {"result": None, "error": None, "zones": time_zones()}
    empty["review"] = await s.nibepi_review(who) if who.principal.allows("settings.read") else None
    token = request.query_params.get("draft")
    if not token:
        return {"draft": None, "token": None, "kept": None, **empty}
    try:
        kept = s.nibepi_draft(who, token)
    except AccountError:
        return {"draft": None, "token": None, "kept": None, **empty}
    return {"draft": kept.draft, "token": token, "kept": kept, **empty}


CONTEXTS = {
    "house": _house,
    "pump": _pump,
    "external": _external,
    "sensors": _sensors,
    "prices": _prices,
    "weather": _weather,
    "control": _control,
    "nibepi": _nibepi,
}


async def show(
    request: Request, who: Caller, topic: str, status_code: int = 200, **extra: Any
) -> Response:
    context = await CONTEXTS[topic](request, who)
    return render(
        request,
        f"setup-{topic}.html",
        who,
        status_code=status_code,
        topic=topic,
        topics=TOPICS,
        **{**context, **extra},
    )


async def attempt(
    request: Request, who: Caller, work: Awaitable[Any], topic: str, then: str | None = None
) -> Response:
    """Run a change; on a refusal, show the topic again with why; else go back to it."""
    try:
        await work
    except NeedsConfirmation:
        raise
    except AccountError as e:
        return await show(request, who, topic, 400, error=_message(e))
    return back(then or f"/setup/{topic}")


@router.get("/setup/{topic}")
async def setup_topic(request: Request, who: Logged, topic: str) -> Response:
    if topic not in TOPICS:
        return render(request, "error.html", who, status_code=404, error=None)
    return await show(request, who, topic)


# The pages these replaced, for bookmarks.


@router.get("/settings")
async def old_settings(_: Logged) -> Response:
    return back("/setup/house")


@router.get("/sensors")
async def old_sensors(_: Logged) -> Response:
    return back("/setup/sensors")


@router.get("/rooms")
async def old_rooms(_: Logged) -> Response:
    return back("/setup/house#rooms")


@router.get("/diagnostics")
async def old_diagnostics(_: Logged) -> Response:
    return back("/system/health")


# --- from NibePi ---------------------------------------------------------------------------

UPLOAD_MOST = 1_000_000
"""A config.json is a few kilobytes; anything far larger is something else."""


@router.post("/setup/nibepi")
@action("nibepi_import.read")
async def read_nibepi(request: Request, who: Logged) -> Response:
    """Read an uploaded config.json into a draft, shown for checking."""
    async with request.form() as form:
        upload = form.get("config")
        data = (
            await upload.read(UPLOAD_MOST + 1)
            if upload is not None and not isinstance(upload, str)
            else None
        )
    if data is None:
        return await show(
            request, who, "nibepi", 400, error=_message(AccountError("choose the file"))
        )
    if len(data) > UPLOAD_MOST:
        return await show(
            request, who, "nibepi", 400, error=_message(AccountError("that file is too large"))
        )
    try:
        token, _ = await services(request).read_nibepi(who, data.decode("utf-8", errors="replace"))
    except AccountError as e:
        return await show(request, who, "nibepi", 400, error=_message(e))
    return back(f"/setup/nibepi?draft={token}#draft")


@router.post("/setup/nibepi/{token}/confirm")
@action("nibepi_import.apply")
async def apply_nibepi(request: Request, who: Logged, token: str) -> Response:
    """Make the ticked parts of a draft."""
    form = await request.form()
    chosen = [i for i in form.getlist("items") if isinstance(i, str)]
    timezone = form.get("timezone")
    model = form.get("model")
    try:
        result = await services(request).apply_nibepi(
            who,
            token,
            chosen,
            timezone=timezone if isinstance(timezone, str) and timezone else None,
            model=model.strip() if isinstance(model, str) and model.strip() else None,
        )
    except NeedsConfirmation:
        raise
    except AccountError as e:
        return await show(request, who, "nibepi", 400, error=_message(e))
    return await show(request, who, "nibepi", result=result)


REVIEW = "/setup/nibepi#review"


@router.post("/setup/nibepi/stopped")
@action("nibepi_import.stopped")
async def nibepi_stopped(request: Request, who: Logged) -> Response:
    """The household says NibePi is stopped."""
    work = services(request).nibepi_stopped(who)
    return await attempt(request, who, work, "nibepi", REVIEW)


@router.post("/setup/nibepi/read")
@action("nibepi_import.review_read")
async def read_pump_settings(request: Request, who: Logged) -> Response:
    """Read the listed settings from the pump; with `everything`, every one of them."""
    everything = (await request.form()).get("everything") == "1"
    work = services(request).read_pump_settings(who, everything=everything)
    return await attempt(request, who, work, "nibepi", REVIEW)


@router.post("/setup/nibepi/write")
@action("nibepi_import.review_write")
async def review_write(request: Request, who: Logged) -> Response:
    """Change one of the pump's settings from the review."""
    from .control_operations import form_number

    form = await request.form()
    register, value = form.get("register"), form.get("value")
    number = form_number(value) if isinstance(value, str) else None
    if not isinstance(register, str) or not register.isdigit() or not isinstance(number, float):
        error = AccountError("give the setting a number")
        return await show(request, who, "nibepi", 400, error=_message(error))
    work = services(request).review_write(who, int(register), number)
    return await attempt(request, who, work, "nibepi", REVIEW)


@router.post("/setup/nibepi/baseline")
@action("lever.baseline")
async def review_baseline(request: Request, who: Logged) -> Response:
    """Use a value as what one of Thermaestro's settings is put back to."""
    form = await request.form()
    lever, value = form.get("lever"), form.get("value")
    if not isinstance(lever, str) or not isinstance(value, str):
        error = AccountError("choose a value")
        return await show(request, who, "nibepi", 400, error=_message(error))
    work = services(request).set_baseline(who, lever, value)
    return await attempt(request, who, work, "nibepi", REVIEW)
