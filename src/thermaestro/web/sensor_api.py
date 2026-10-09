"""The API for sensors, rooms, the outdoor references, names, the MQTT broker and Home
Assistant."""

from dataclasses import asdict
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from .app import action, caller, services
from .operations import Caller

router = APIRouter(prefix="/api/v1")

Logged = Annotated[Caller, Depends(caller)]


class NameIt(BaseModel):
    ref: str = Field(description="`<instance>:<path>` of a point or node")
    name: str | None = Field(None, description="empty or null: the built-in name again")


class HomeAssistantConnection(BaseModel):
    url: str
    token: str | None = Field(
        None, description="a new long-lived token; null keeps the one entered"
    )


class Pick(BaseModel):
    entity: str
    point: str
    quantity: str
    name: str
    room: str | None = None
    new_room: str | None = Field(
        None,
        description="instead of `room`: a room's name, made where no room has it (such as"
        " the entity's area)",
    )
    placement: str = "other"


class Areas(BaseModel):
    areas: list[str] = Field(description="Home Assistant's area names, each to become a room")


@router.get("/site")
@action("site.read")
async def site(request: Request, who: Logged) -> dict[str, Any]:
    """Rooms and the outdoors, as derived from the sensors."""
    return services(request).site(who)


# --- sensors -----------------------------------------------------------------------------


@router.get("/sensors")
@action("sensors.read")
async def sensors(request: Request, who: Logged) -> dict[str, Any]:
    s = services(request)
    readings = s.sensor_readings(who)
    return {
        id: {**sensor.model_dump(mode="json"), "reading": readings.get(id)}
        for id, sensor in (await s.sensor_settings(who)).items()
    }


@router.post("/sensors", status_code=201)
@action("sensor.write")
async def add_sensor(body: dict[str, Any], request: Request, who: Logged) -> dict[str, Any]:
    id, sensor = await services(request).set_sensor(who, None, body)
    return {"id": id, **sensor.model_dump(mode="json")}


@router.put("/sensors/{id}")
@action("sensor.write")
async def set_sensor(
    id: str, body: dict[str, Any], request: Request, who: Logged
) -> dict[str, Any]:
    _, sensor = await services(request).set_sensor(who, id, body)
    return {"id": id, **sensor.model_dump(mode="json")}


@router.delete("/sensors/{id}")
@action("sensor.delete")
async def delete_sensor(id: str, request: Request, who: Logged) -> dict[str, str]:
    await services(request).delete_sensor(who, id)
    return {"status": "ok"}


# --- rooms -------------------------------------------------------------------------------


@router.get("/rooms")
@action("rooms.read")
async def rooms(request: Request, who: Logged) -> dict[str, Any]:
    return {
        id: r.model_dump(mode="json")
        for id, r in (await services(request).room_settings(who)).items()
    }


@router.post("/rooms", status_code=201)
@action("room.write")
async def add_room(body: dict[str, Any], request: Request, who: Logged) -> dict[str, Any]:
    id, room = await services(request).set_room(who, None, body)
    return {"id": id, **room.model_dump(mode="json")}


@router.put("/rooms/{id}")
@action("room.write")
async def set_room(id: str, body: dict[str, Any], request: Request, who: Logged) -> dict[str, Any]:
    _, room = await services(request).set_room(who, id, body)
    return {"id": id, **room.model_dump(mode="json")}


@router.delete("/rooms/{id}")
@action("room.delete")
async def delete_room(id: str, request: Request, who: Logged) -> dict[str, str]:
    await services(request).delete_room(who, id)
    return {"status": "ok"}


# --- the outdoor references and names ----------------------------------------------------


@router.get("/outdoor")
@action("outdoor.read")
async def outdoor(request: Request, who: Logged) -> dict[str, str]:
    return dict((await services(request).outdoor(who)).references)


@router.put("/outdoor")
@action("outdoor.write")
async def set_outdoor(body: dict[str, str], request: Request, who: Logged) -> dict[str, str]:
    """Quantity to sensor id: `{"temperature": "north-wall"}`."""
    return dict((await services(request).set_outdoor(who, body)).references)


@router.get("/names")
@action("names.read")
async def names(request: Request, who: Logged) -> dict[str, str]:
    return await services(request).names(who)


@router.put("/names")
@action("name.write")
async def set_name(body: NameIt, request: Request, who: Logged) -> dict[str, str]:
    await services(request).set_name(who, body.ref, body.name)
    return {"status": "ok"}


# --- the MQTT broker and Home Assistant --------------------------------------------------


class Showing(BaseModel):
    ref: str = Field(description="`<instance>:<path>` of a point")
    category: str | None = Field(
        None, description="primary, config or diagnostic; null: as its plugin says"
    )
    pinned: bool | None = Field(None, description="on the overview's cards; null: unchanged")


@router.get("/display")
@action("display.read")
async def display(request: Request, who: Logged) -> dict[str, Any]:
    """The household's choices: points moved to another category, and those pinned."""
    who.principal.require("points.read")
    hub = services(request).sensors
    return hub.display.model_dump(mode="json") if hub else {"categories": {}, "pinned": []}


@router.put("/display")
@action("display.write")
async def set_display(body: Showing, request: Request, who: Logged) -> dict[str, str]:
    await services(request).set_display(who, body.ref, body.category, body.pinned)
    return {"status": "ok"}


@router.get("/mqtt")
@action("mqtt.read")
async def mqtt(request: Request, who: Logged) -> dict[str, Any]:
    s = services(request)
    found = await s.mqtt_settings(who)
    return {"settings": found.model_dump(mode="json") if found else None, **s.mqtt_state(who)}


@router.put("/mqtt")
@action("mqtt.write")
async def set_mqtt(body: dict[str, Any], request: Request, who: Logged) -> dict[str, Any]:
    """The broker; a password goes in first through `PUT /api/v1/secrets/mqtt.password`."""
    return (await services(request).set_mqtt(who, body)).model_dump(mode="json")


@router.get("/discovery")
@action("discovery.read")
async def discovery(request: Request, who: Logged) -> dict[str, Any]:
    """Home Assistant discovery: the setting, and what is published now."""
    s = services(request)
    found = await s.discovery_settings(who)
    return {"settings": found.model_dump(mode="json"), **s.discovery_state(who)}


@router.put("/discovery")
@action("discovery.write")
async def set_discovery(body: dict[str, Any], request: Request, who: Logged) -> dict[str, Any]:
    """Switch it on or off (`enabled`), or change `prefix`, `base`, `sensors` or
    `language`; the installation's `id` is made when first switched on."""
    return (await services(request).set_discovery(who, body)).model_dump(mode="json")


@router.put("/homeassistant/{id}")
@action("homeassistant.write")
async def set_home_assistant(
    id: str, body: HomeAssistantConnection, request: Request, who: Logged
) -> dict[str, Any]:
    plugin = await services(request).set_home_assistant(who, id, body.url, body.token or "")
    return plugin.model_dump(mode="json")


@router.get("/homeassistant/{id}/entities")
@action("homeassistant.entities")
async def home_assistant_entities(id: str, request: Request, who: Logged) -> list[dict[str, Any]]:
    return [asdict(e) for e in await services(request).home_assistant_entities(who, id)]


@router.post("/homeassistant/{id}/sensors", status_code=201)
@action("homeassistant.sensors")
async def add_home_assistant_sensors(
    id: str, picks: list[Pick], request: Request, who: Logged
) -> dict[str, list[str]]:
    """Read these entities, with a sensor made of each point."""
    made = await services(request).add_home_assistant_sensors(
        who, id, [p.model_dump() for p in picks]
    )
    return {"sensors": made}


@router.get("/homeassistant/{id}/areas")
@action("homeassistant.areas")
async def home_assistant_areas(id: str, request: Request, who: Logged) -> list[str]:
    """The areas of Home Assistant's entities that aren't a room yet."""
    return await services(request).home_assistant_areas(who, id)


@router.post("/homeassistant/{id}/rooms", status_code=201)
@action("homeassistant.rooms")
async def rooms_from_areas(
    id: str, body: Areas, request: Request, who: Logged
) -> dict[str, list[str]]:
    """A room of each area. Once made, a room is Thermaestro's own: renaming or removing
    the area in Home Assistant doesn't change it."""
    return {"rooms": await services(request).rooms_from_areas(who, id, body.areas)}
