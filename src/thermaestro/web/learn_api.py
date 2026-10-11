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


class Offered(BaseModel):
    id: str
    """An offer's id, as listed under `offers`."""


@router.post("/learned/offers/accept")
@action("learned_offer.accept")
async def accept_offer(body: Offered, request: Request, who: Logged) -> dict[str, Any]:
    """Ask for a suggested hot-water time: as asking for its `request` does, with the
    caller's rights. Refused (400) unless it was accepted."""
    return await services(request).accept_offer(who, body.id)


@router.post("/learned/offers/dismiss")
@action("learned_offer.dismiss")
async def dismiss_offer(body: Offered, request: Request, who: Logged) -> dict[str, str]:
    """Not now: the suggestion isn't made again for eight weeks."""
    await services(request).dismiss_offer(who, body.id)
    return {"status": "ok"}


class Profile(BaseModel):
    profile: str
    """The text of NibePi's vv_ai_profile.json."""
    timezone: str | None = None
    """The time zone NibePi ran in; the house's if left out."""
    tank: str | None = None
    """The tank it is for, `instance:path`; the first if left out."""


@router.post("/import/nibepi/profile/read")
@action("nibepi_profile.read")
async def read_profile(body: Profile, request: Request, who: Logged) -> dict[str, Any]:
    """Read NibePi's hot-water profile into the tank's week as it would start, kept for this
    user for half an hour. Nothing changes."""
    from .learn_operations import week_rows

    s = services(request)
    token, kept = await s.read_profile(who, body.profile, timezone=body.timezone, tank=body.tank)
    return {
        "token": token,
        "tank": kept.scope,
        "timezone": kept.timezone,
        "empty": kept.empty,
        "week": kept.week.to_value(),
        "rows": week_rows(kept.week),
    }


@router.post("/import/nibepi/profile/{token}")
@action("nibepi_profile.apply")
async def apply_profile(token: str, request: Request, who: Logged) -> dict[str, str]:
    """Start the tank's week from the profile read. Refused once the week has been learned
    from a day or more: start it over first."""
    await services(request).apply_profile(who, token)
    return {"status": "ok"}


@router.put("/learned/pin")
@action("learned.pin")
async def pin(body: Pin, request: Request, who: Logged) -> dict[str, Any]:
    """Use a number instead of what was learned, or lift the pin."""
    return await services(request).pin_learned(who, body.key, body.value)
