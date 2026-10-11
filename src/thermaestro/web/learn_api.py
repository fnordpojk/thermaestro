"""The API for what Thermaestro has learned, and which inputs work here."""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

from .app import action, caller, services
from .operations import Caller

router = APIRouter(prefix="/api/v1")

Logged = Annotated[Caller, Depends(caller)]


class Named(BaseModel):
    key: str
    """A learned value's key, as listed: `tank.charge_s@nibe:hp1/dhw`."""


class Pin(Named):
    value: float | None
    """The number to use instead of what was learned; null lifts the pin."""


@router.get("/learned")
@action("learned.read")
async def learned(request: Request, who: Logged) -> dict[str, Any]:
    """Which inputs work here (`inputs`: each point's verdict, `usable`, `not_usable` or
    `not_yet`, and why), and what has been learned (`values`)."""
    return await services(request).learned(who)


@router.post("/learned/inputs/check")
@action("learned_inputs.check")
async def check_inputs(request: Request, who: Logged) -> list[dict[str, Any]]:
    """Judge the inputs over the last week now, rather than at the next hourly round."""
    return await services(request).check_inputs(who)


@router.post("/learned/start-over")
@action("learned.start_over")
async def start_over(body: Named, request: Request, who: Logged) -> dict[str, Any]:
    """Forget a learned value; its starting value stands until it is learned again."""
    return await services(request).start_over(who, body.key)


@router.put("/learned/pin")
@action("learned.pin")
async def pin(body: Pin, request: Request, who: Logged) -> dict[str, Any]:
    """Use a number instead of what was learned, or lift the pin."""
    return await services(request).pin_learned(who, body.key, body.value)
