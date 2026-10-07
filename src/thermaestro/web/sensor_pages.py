"""The pages for sensors, rooms, the outdoor references and names; each form posts to
an endpoint that does what its API counterpart does."""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, Response

from ..auth import AccountError
from ..cap.vocabulary import QUANTITIES, STATES
from ..store import Plugin
from .app import action, caller, local_path, services
from .operations import Caller, NeedsConfirmation
from .pages import Text, back, render
from .pages import _message as message
from .sensor_operations import HA_PLUGIN

router = APIRouter(default_response_class=HTMLResponse)

Logged = Annotated[Caller, Depends(caller)]

ROOM_QUANTITIES = ("heat_demand", "setpoint", "zone.open", "window.open")
SENSOR_QUANTITIES = (*QUANTITIES, *ROOM_QUANTITIES, *sorted(STATES))
MQTT_PASSWORD = "mqtt.password"  # noqa: S105 - the name of the secret, not the secret
OUTDOOR_QUANTITIES = ("temperature", "humidity", "atmospheric_pressure", "irradiance", "pm25")


# --- the sensors page --------------------------------------------------------------------


async def _sensors(request: Request, who: Caller, status_code: int = 200, **extra: Any) -> Response:
    s = services(request)
    sensors = await s.sensor_settings(who)
    plugins = await s.plugins(who)
    connections = {id: p for id, p in plugins.items() if p.plugin == HA_PLUGIN}
    return render(
        request,
        "sensors.html",
        who,
        status_code=status_code,
        sensors=dict(sorted(sensors.items(), key=lambda kv: kv[1].name.lower())),
        readings=s.sensor_readings(who),
        rooms=await s.room_settings(who),
        outdoor=(await s.outdoor(who)).references,
        outdoor_quantities=OUTDOOR_QUANTITIES,
        quantities=SENSOR_QUANTITIES,
        connections=connections,
        has_broker=await s.mqtt_settings(who) is not None,
        **extra,
    )


@router.get("/sensors")
async def sensors_page(request: Request, who: Logged) -> Response:
    return await _sensors(request, who)


async def _attempt(request: Request, who: Caller, work: Any, then: str) -> Response:
    """Run a change; on a refusal, show the sensors page again with why."""
    try:
        await work
    except NeedsConfirmation:
        raise
    except AccountError as e:
        return await _sensors(request, who, 400, error=message(e))
    return back(then)


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

    return await _settings_attempt(request, who, work(), "/settings#mqtt")


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
    return await _settings_attempt(
        request, who, services(request).set_discovery(who, body), "/settings#discovery"
    )


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


async def _settings_attempt(request: Request, who: Caller, work: Any, then: str) -> Response:
    """Run a change from the settings page; on a refusal, show it again with why."""
    from .pages import _settings

    try:
        await work
    except NeedsConfirmation:
        raise
    except AccountError as e:
        return await _settings(request, who, 400, error=message(e))
    return back(then)


@router.post("/sensors")
@action("sensor.write")
async def add_sensor(request: Request, who: Logged) -> Response:
    form = await _form(request)
    try:
        body = _sensor_body(form)
    except ValueError:
        return await _sensors(request, who, 400, error=message(AccountError("not a number")))
    return await _attempt(request, who, services(request).set_sensor(who, None, body), "/sensors")


@router.post("/sensors/outdoor")
@action("outdoor.write")
async def set_outdoor(request: Request, who: Logged) -> Response:
    form = await _form(request)
    references = {q: form.get(q, "") for q in OUTDOOR_QUANTITIES}
    return await _attempt(
        request, who, services(request).set_outdoor(who, references), "/sensors#outdoor"
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

    return await _settings_attempt(request, who, work(), f"/sensors/homeassistant/{id}")


@router.get("/sensors/homeassistant/{id}")
async def home_assistant_page(request: Request, who: Logged, id: str) -> Response:
    s = services(request)
    error = None
    entities: list[Any] = []
    try:
        entities = await s.home_assistant_entities(who, id)
    except AccountError as e:
        error = message(e)
    plugin = await s.db.get(Plugin, id)
    listed = plugin.settings.get("entities") if plugin else None
    chosen = {e for e in listed if isinstance(e, str)} if isinstance(listed, list) else set()
    rooms = await s.room_settings(who)
    by_name = {r.name.lower(): rid for rid, r in rooms.items()}
    return render(
        request,
        "ha-entities.html",
        who,
        id=id,
        entities=entities,
        chosen=chosen,
        rooms=rooms,
        suggested_room={e.entity_id: by_name.get((e.area or "").lower()) for e in entities},
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
    return await _attempt(request, who, work, "/sensors")


@router.post("/sensors/{id}")
@action("sensor.write")
async def set_sensor(request: Request, who: Logged, id: str) -> Response:
    form = await _form(request)
    try:
        body = _sensor_body(form)
    except ValueError:
        return await _sensors(request, who, 400, error=message(AccountError("not a number")))
    return await _attempt(request, who, services(request).set_sensor(who, id, body), "/sensors")


@router.post("/sensors/{id}/delete")
@action("sensor.delete")
async def delete_sensor(request: Request, who: Logged, id: str) -> Response:
    return await _attempt(request, who, services(request).delete_sensor(who, id), "/sensors")


# --- rooms -------------------------------------------------------------------------------


async def _rooms(request: Request, who: Caller, status_code: int = 200, **extra: Any) -> Response:
    s = services(request)
    systems = []
    for instance_id, instance in sorted((s.host.instances if s.host else {}).items()):
        described = instance.described
        for node in described.nodes if described else ():
            if node.kind == "climate_system":
                label = s.node_label(who, instance_id, node.path)
                systems.append((f"{instance_id}:{node.path}", label))
    return render(
        request,
        "rooms.html",
        who,
        status_code=status_code,
        rooms=dict(sorted((await s.room_settings(who)).items(), key=lambda kv: kv[1].name.lower())),
        sensors=await s.sensor_settings(who),
        systems=systems,
        **extra,
    )


@router.get("/rooms")
async def rooms_page(request: Request, who: Logged) -> Response:
    return await _rooms(request, who)


async def _room_attempt(request: Request, who: Caller, work: Any) -> Response:
    try:
        await work
    except AccountError as e:
        return await _rooms(request, who, 400, error=message(e))
    return back("/rooms")


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
    return await _room_attempt(request, who, services(request).set_room(who, None, body))


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
    return await _room_attempt(request, who, services(request).set_room(who, id, body))


@router.post("/rooms/{id}/delete")
@action("room.delete")
async def delete_room(request: Request, who: Logged, id: str) -> Response:
    return await _room_attempt(request, who, services(request).delete_room(who, id))


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
