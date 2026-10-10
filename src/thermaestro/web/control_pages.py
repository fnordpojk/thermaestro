"""The forms for control: each lever's mode, its competing features confirmed off, a
change made elsewhere kept; and setup's answers about the house. Each posts to the same
operations as the API, and shows its setup topic again on a refusal."""

from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, Response

from .app import action, caller, services
from .operations import Caller

router = APIRouter(default_response_class=HTMLResponse)

Logged = Annotated[Caller, Depends(caller)]
Text = Annotated[str, Form()]


@router.post("/levers/{ref:path}/mode")
@action("lever.mode")
async def set_mode(request: Request, who: Logged, ref: str, mode: Text) -> Response:
    from .setup_pages import attempt

    work = services(request).set_lever_mode(who, ref, mode)
    return await attempt(request, who, work, "control")


@router.post("/levers/{ref:path}/confirmed-off")
@action("lever.confirm_off")
async def confirm_off(request: Request, who: Logged, ref: str) -> Response:
    from .setup_pages import attempt

    features = [f for f in (await request.form()).getlist("features") if isinstance(f, str)]
    work = services(request).confirm_lever_off(who, ref, features)
    return await attempt(request, who, work, "control")


@router.post("/levers/{ref:path}/accept-drift")
@action("lever.accept_drift")
async def accept_drift(request: Request, who: Logged, ref: str) -> Response:
    from .setup_pages import attempt

    return await attempt(request, who, services(request).accept_drift(who, ref), "control")


@router.post("/settings/home")
@action("home.write")
async def set_home(request: Request, who: Logged) -> Response:
    """Setup's answers about the house. A climate system's emitter comes as
    `emitter:<its node>`."""
    from .setup_pages import attempt

    form = await request.form()
    emitters = {
        key.removeprefix("emitter:"): value
        for key, value in form.items()
        if key.startswith("emitter:") and isinstance(value, str)
    }
    body: dict[str, object] = {"emitters": emitters}
    for name in ("house", "water", "past_deadline"):
        value = form.get(name)
        if isinstance(value, str) and value:
            body[name] = value
    holidays = form.get("holidays")
    body["holidays"] = (
        holidays.strip().upper() if isinstance(holidays, str) and holidays.strip() else None
    )
    holidays_as = form.get("holidays_as")
    body["holidays_as"] = (
        int(holidays_as) if isinstance(holidays_as, str) and holidays_as.isdigit() else None
    )
    return await attempt(
        request, who, services(request).set_home(who, body), "house", "/setup/house#home"
    )
