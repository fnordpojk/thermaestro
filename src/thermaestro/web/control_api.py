"""The API for control: intents and levels, setup's answers about the house, the levers
and their modes, and the plan."""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

from .app import action, caller, services
from .operations import Caller

router = APIRouter(prefix="/api/v1")

Logged = Annotated[Caller, Depends(caller)]


class Mode(BaseModel):
    mode: str


class Features(BaseModel):
    features: list[str]


# --- intents ------------------------------------------------------------------------------


@router.get("/intents")
@action("intents.read")
async def intents(request: Request, who: Logged, ended: bool = False) -> list[dict[str, Any]]:
    """The open intents; with `ended`, the finished ones too."""
    found = await services(request).intent_list(who, ended=ended)
    return [i.model_dump(mode="json") for i in found]


@router.post("/intents", status_code=201)
@action("intent.ask")
async def ask(body: dict[str, Any], request: Request, who: Logged) -> dict[str, Any]:
    """Ask for something in household terms, such as `{"kind": "warmer", "scope":
    "pump:hp1/cs1", "offset": 1}` or `{"kind": "bath", "scope": "pump:hp1/dhw",
    "at_least": 50, "by": "2026-10-10T19:30:00+02:00"}`. The answer says whether it was
    accepted, with the intent as kept and what it means."""
    return await services(request).ask(who, body)


@router.delete("/intents/{id}")
@action("intent.end")
async def end(id: str, request: Request, who: Logged) -> dict[str, Any]:
    return (await services(request).end_intent(who, id)).model_dump(mode="json")


@router.post("/intents/{id}/confirm")
@action("intent.confirm")
async def confirm(id: str, request: Request, who: Logged) -> dict[str, Any]:
    """A seeded intent becomes the household's own."""
    return (await services(request).confirm_intent(who, id)).model_dump(mode="json")


@router.get("/intents/in-force")
@action("intents_in_force.read")
async def in_force(request: Request, who: Logged) -> dict[str, Any]:
    return await services(request).in_force(who)


@router.get("/levels")
@action("levels.read")
async def levels(request: Request, who: Logged) -> list[dict[str, Any]]:
    found = await services(request).level_list(who)
    return [level.model_dump(mode="json") for level in found.values()]


@router.put("/levels/{id}")
@action("level.write")
async def put_level(id: str, body: dict[str, Any], request: Request, who: Logged) -> dict[str, Any]:
    """`{"name": "Day", "scope": "pump:hp1/cs1", "low": 20.5, "high": 22}`, or a tank's
    `{"name": "Normal", "scope": "pump:hp1/dhw", "top": 50}`."""
    return (await services(request).put_level(who, id, body)).model_dump(mode="json")


@router.delete("/levels/{id}")
@action("level.delete")
async def delete_level(id: str, request: Request, who: Logged) -> dict[str, str]:
    await services(request).delete_level(who, id)
    return {"status": "ok"}


# --- setup's answers about the house ------------------------------------------------------


@router.get("/home")
@action("home.read")
async def home(request: Request, who: Logged) -> dict[str, Any]:
    return (await services(request).home_settings(who)).model_dump(mode="json")


@router.put("/home")
@action("home.write")
async def set_home(body: dict[str, Any], request: Request, who: Logged) -> dict[str, Any]:
    """`{"emitters": {"pump:hp1/cs1": "floor"}, "house": "average", "water": "well",
    "holidays": "SE", "holidays_as": 6, "past_deadline": "keep_heating"}`."""
    return (await services(request).set_home(who, body)).model_dump(mode="json")


# --- levers -------------------------------------------------------------------------------


@router.get("/levers")
@action("levers.read")
async def levers(request: Request, who: Logged) -> list[dict[str, Any]]:
    return await services(request).levers(who)


@router.put("/levers/{ref:path}/mode")
@action("lever.mode")
async def set_mode(ref: str, body: Mode, request: Request, who: Logged) -> dict[str, str]:
    """`{"mode": "shadow"}`: off, shadow or control. Leaving control puts the setting back
    as it was found."""
    await services(request).set_lever_mode(who, ref, body.mode)
    return {"status": "ok"}


@router.put("/levers/{ref:path}/confirmed-off")
@action("lever.confirm_off")
async def confirm_off(ref: str, body: Features, request: Request, who: Logged) -> dict[str, str]:
    """`{"features": ["Smart Price Adaption"]}`: these competing features are switched off."""
    await services(request).confirm_lever_off(who, ref, body.features)
    return {"status": "ok"}


@router.post("/levers/{ref:path}/accept-drift")
@action("lever.accept_drift")
async def accept_drift(ref: str, request: Request, who: Logged) -> dict[str, str]:
    """Keep the change made elsewhere."""
    await services(request).accept_drift(who, ref)
    return {"status": "ok"}


# --- the plan -----------------------------------------------------------------------------


@router.get("/plan")
@action("plan.read")
async def plan(request: Request, who: Logged) -> dict[str, Any]:
    """The planner's last round, each decision with its reason and outcome; and in shadow,
    what it would have done."""
    return services(request).plan(who)


@router.get("/plan/changes")
@action("plan_changes.read")
async def changes(request: Request, who: Logged, hours: float = 24.0) -> list[dict[str, Any]]:
    """Every change asked in the last `hours` (at most a week), newest first: made, shadowed
    or refused, with who asked, why, and what became of it."""
    return await services(request).recent_acts(who, min(max(hours, 0.0), 168.0))
