"""The forms for sensors, rooms, the MQTT broker, Home Assistant, publishing to it, what's
shown where, and names. Each posts to an endpoint that does what its API counterpart
does, and goes back to its topic under Setup."""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, Response

from ..auth import AccountError
from ..cap.vocabulary import QUANTITIES, STATES
from ..store import Plugin
from .app import action, caller, local_path, services
from .operations import Caller
from .pages import Text, back, render
from .pages import _message as message
from .setup_pages import OUTDOOR_QUANTITIES, attempt, show

router = APIRouter(default_response_class=HTMLResponse)

Logged = Annotated[Caller, Depends(caller)]

ROOM_QUANTITIES = ("heat_demand", "setpoint", "zone.open", "window.open")
SENSOR_QUANTITIES = (*QUANTITIES, *ROOM_QUANTITIES, *sorted(STATES))
MQTT_PASSWORD = "mqtt.password"  # noqa: S105 - the name of the secret, not the secret


# --- External: the MQTT broker, Home Assistant, publishing --------------------------------


@router.post("/settings/mqtt")
@action("mqtt.write")
async def set_mqtt(
    request: Request,
    who: Logged,
    host: Text,
    port: Text = "1883",
    username: Text = "",
    password: Text = "",
    tls: Text = "",
) -> Response:
    s = services(request)
    body: dict[str, Any] = {
        "host": host.strip(),
        "port": port,
        "username": username.strip() or None,
        "tls": bool(tls),
    }
    current = await s.mqtt_settings(who)

    async def work() -> None:
        if password:
            await s.set_secret(who, MQTT_PASSWORD, password)
            body["password"] = MQTT_PASSWORD
        elif current is not None and current.password:
            body["password"] = current.password
        await s.set_mqtt(who, body)

    return await attempt(request, who, work(), "external", "/setup/external#mqtt")


@router.post("/settings/discovery")
@action("discovery.write")
async def set_discovery(
    request: Request,
    who: Logged,
    prefix: Text = "homeassistant",
    base: Text = "thermaestro",
    language: Text = "en",
    enabled: Text = "",
    sensors: Text = "",
) -> Response:
    body = {
        "enabled": bool(enabled),
        "prefix": prefix.strip(),
        "base": base.strip(),
        "language": language,
        "sensors": bool(sensors),
    }
    return await attempt(
        request,
        who,
        services(request).set_discovery(who, body),
        "external",
        "/setup/external#discovery",
    )


@router.post("/settings/homeassistant")
@action("homeassistant.write")
async def set_home_assistant(
    request: Request, who: Logged, url: Text, id: Text = "homeassistant", token: Text = ""
) -> Response:
    s = services(request)
    token_name = f"{id.lower()}.token"

    async def work() -> None:
        if token:
            await s.set_secret(who, token_name, token.strip())
        await s.set_home_assistant(who, id, url, token_name)

    return await attempt(request, who, work(), "external", f"/sensors/homeassistant/{id}")


# --- Sensors ------------------------------------------------------------------------------


def _sensor_body(form: dict[str, str]) -> dict[str, Any]:
    placement = form.get("placement") or "room"
    body: dict[str, Any] = {
        "name": form.get("name", ""),
        "source": form.get("source") or "mqtt",
        "quantity": form.get("quantity") or "temperature",
        "placement": placement,
        "room": (form.get("room") or None) if placement == "room" else None,
        "reference": bool(form.get("reference")) and placement == "room",
        "calibration_offset": form.get("calibration_offset") or 0,
    }
    if form.get("freshness_min"):
        body["freshness_s"] = float(form["freshness_min"]) * 60
    if body["source"] == "mqtt":
        body["topic"] = form.get("topic", "").strip()
        body["json_key"] = form.get("json_key", "").strip() or None
    else:
        body["point"] = form.get("point", "").strip()
    return body


async def _form(request: Request) -> dict[str, str]:
    data = await request.form()
    return {k: v for k, v in data.items() if isinstance(v, str) and k != "csrf"}


@router.post("/sensors")
@action("sensor.write")
async def add_sensor(request: Request, who: Logged) -> Response:
    form = await _form(request)
    try:
        body = _sensor_body(form)
    except ValueError:
        return await show(request, who, "sensors", 400, error=message(AccountError("not a number")))
    return await attempt(request, who, services(request).set_sensor(who, None, body), "sensors")


@router.post("/sensors/outdoor")
@action("outdoor.write")
async def set_outdoor(request: Request, who: Logged) -> Response:
    form = await _form(request)
    references = {q: form.get(q, "") for q in OUTDOOR_QUANTITIES}
    return await attempt(
        request,
        who,
        services(request).set_outdoor(who, references),
        "sensors",
        "/setup/sensors#outdoor",
    )


async def _entities(request: Request, who: Caller, id: str) -> tuple[list[Any], str | None]:
    try:
        return await services(request).home_assistant_entities(who, id), None
    except AccountError as e:
        return [], message(e)


@router.get("/sensors/homeassistant/{id}")
async def home_assistant_page(request: Request, who: Logged, id: str) -> Response:
    """The entities to tick. The list sends only the ticks (a GET), so it stays small
    however many entities Home Assistant has."""
    entities, error = await _entities(request, who, id)
    plugin = await services(request).db.get(Plugin, id)
    listed = plugin.settings.get("entities") if plugin else None
    chosen = {e for e in listed if isinstance(e, str)} if isinstance(listed, list) else set()
    return render(
        request,
        "ha-entities.html",
        who,
        id=id,
        entities=entities,
        chosen=chosen,
        error=error,
    )


@router.get("/sensors/homeassistant/{id}/pick")
async def home_assistant_pick_page(request: Request, who: Logged, id: str) -> Response:
    """Names and rooms for the ticked entities."""
    wanted = set(request.query_params.getlist("pick"))
    entities, error = await _entities(request, who, id)
    picks = [
        (e, point, quantity) for e in entities for point, quantity in e.points if point in wanted
    ]
    rooms = await services(request).room_settings(who)
    by_name = {r.name.lower(): rid for rid, r in rooms.items()}
    return render(
        request,
        "ha-pick.html",
        who,
        id=id,
        picks=picks,
        rooms=rooms,
        suggested_room={e.entity_id: by_name.get((e.area or "").lower()) for e, _, _ in picks},
        error=error,
    )


@router.post("/sensors/homeassistant/{id}/sensors")
@action("homeassistant.sensors")
async def add_home_assistant_sensors(request: Request, who: Logged, id: str) -> Response:
    form = await _form(request)
    data = await request.form()
    picked = [v for v in data.getlist("pick") if isinstance(v, str)]
    picks = []
    for n in picked:
        quantity = form.get(f"quantity_{n}", "")
        room = form.get(f"room_{n}") or None
        picks.append(
            {
                "entity": form.get(f"entity_{n}", ""),
                "point": form.get(f"point_{n}", ""),
                "quantity": quantity,
                "name": form.get(f"name_{n}", "").strip() or form.get(f"entity_{n}", ""),
                "room": room,
                "placement": "room" if room else form.get(f"placement_{n}") or "other",
            }
        )
    work = services(request).add_home_assistant_sensors(who, id, picks)
    return await attempt(request, who, work, "sensors")


@router.post("/sensors/{id}")
@action("sensor.write")
async def set_sensor(request: Request, who: Logged, id: str) -> Response:
    form = await _form(request)
    try:
        body = _sensor_body(form)
    except ValueError:
        return await show(request, who, "sensors", 400, error=message(AccountError("not a number")))
    return await attempt(request, who, services(request).set_sensor(who, id, body), "sensors")


@router.post("/sensors/{id}/delete")
@action("sensor.delete")
async def delete_sensor(request: Request, who: Logged, id: str) -> Response:
    return await attempt(request, who, services(request).delete_sensor(who, id), "sensors")


# --- rooms, under House -------------------------------------------------------------------


@router.post("/rooms")
@action("room.write")
async def add_room(
    request: Request,
    who: Logged,
    name: Text,
    climate_system: Text = "",
    own_device: Text = "unknown",
) -> Response:
    body = {"name": name, "climate_system": climate_system or None, "own_device": own_device}
    return await attempt(
        request, who, services(request).set_room(who, None, body), "house", "/setup/house#rooms"
    )


@router.post("/rooms/{id}")
@action("room.write")
async def set_room(
    request: Request,
    who: Logged,
    id: str,
    name: Text,
    climate_system: Text = "",
    own_device: Text = "unknown",
) -> Response:
    body = {"name": name, "climate_system": climate_system or None, "own_device": own_device}
    return await attempt(
        request, who, services(request).set_room(who, id, body), "house", "/setup/house#rooms"
    )


@router.post("/rooms/{id}/delete")
@action("room.delete")
async def delete_room(request: Request, who: Logged, id: str) -> Response:
    return await attempt(
        request, who, services(request).delete_room(who, id), "house", "/setup/house#rooms"
    )


# --- showing ---------------------------------------------------------------------------


@router.post("/display")
@action("display.write")
async def set_display(
    request: Request,
    who: Logged,
    ref: Text,
    category: Text = "",
    pinned: Text = "",
    next: Annotated[str, Form()] = "/",
) -> Response:
    try:
        await services(request).set_display(who, ref, category or None, bool(pinned))
    except AccountError as e:
        return render(request, "error.html", who, status_code=400, error=message(e))
    return back(local_path(next))


# --- names -------------------------------------------------------------------------------


@router.post("/names")
@action("name.write")
async def set_name(
    request: Request, who: Logged, ref: Text, name: Text = "", next: Annotated[str, Form()] = "/"
) -> Response:
    try:
        await services(request).set_name(who, ref, name)
    except AccountError as e:
        return render(request, "error.html", who, status_code=400, error=message(e))
    return back(local_path(next))
