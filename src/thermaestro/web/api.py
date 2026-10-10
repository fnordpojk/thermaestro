"""The JSON API, version 1: what the CLI, scripts and later the apps use.

A token goes in `Authorization: Bearer thm_...`. A browser session works too; its unsafe
requests then need the CSRF token in the `X-CSRF-Token` header.
"""

import time
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import BaseModel, Field

from ..auth.accounts import TOKEN_DAYS
from . import operations
from .app import action, anonymous_api, caller, services
from .operations import Caller
from .security import client_address, csrf_token
from .sessions import (
    display_link,
    end_browser_session,
    start_browser_session,
    start_display_session,
)

router = APIRouter(prefix="/api/v1")

Logged = Annotated[Caller, Depends(caller)]
Anonymous = Depends(anonymous_api)


class Login(BaseModel):
    name: str
    password: str


class Setup(BaseModel):
    code: str
    name: str
    password: str


class Confirm(BaseModel):
    password: str


class NewUser(BaseModel):
    name: str
    password: str
    groups: list[str] = []


class Groups(BaseModel):
    groups: list[str]


class Password(BaseModel):
    password: str
    end_sessions: bool = True


class Rights(BaseModel):
    permissions: list[str]


class NewToken(BaseModel):
    name: str = Field(max_length=64)
    permissions: list[str]
    days: int = TOKEN_DAYS


class NewDisplay(BaseModel):
    name: str = Field(max_length=64)
    permissions: list[str]


class OpenDisplay(BaseModel):
    link: str
    """The display's link, or its last part."""


class Secret(BaseModel):
    value: str


# --- logging in --------------------------------------------------------------------------


@router.post("/login", dependencies=[Anonymous])
@action("login")
async def login(body: Login, request: Request, response: Response) -> dict[str, str]:
    """Start a browser-style session; the answer carries its CSRF token."""
    session_hash = await start_browser_session(request, response, body.name, body.password)
    return {"csrf": csrf_token(request.app.state.csrf_key, session_hash)}


@router.post("/logout")
@action("logout")
async def logout(request: Request, response: Response, who: Logged) -> dict[str, str]:
    await end_browser_session(request, response, who)
    return {"status": "ok"}


@router.post("/setup", dependencies=[Anonymous])
@action("setup")
async def setup(body: Setup, request: Request) -> dict[str, str]:
    """Create the first administrator with the setup code."""
    s = services(request)
    user = await s.accounts.create_first_admin(
        s.setup, body.code, body.name, body.password, source=client_address(request)
    )
    return {"user": user.name}


@router.post("/confirm")
@action("confirm")
async def confirm(body: Confirm, request: Request, who: Logged) -> dict[str, str]:
    """Enter the password again, for the changes that need it."""
    if who.session is not None:
        await services(request).accounts.confirm(
            who.session, body.password, source=client_address(request)
        )
    return {"status": "ok"}


# --- status and history ------------------------------------------------------------------


@router.get("/status")
@action("status")
async def status(request: Request, who: Logged) -> list[dict[str, Any]]:
    return services(request).status(who)


@router.get("/history/{instance}/{point:path}")
@action("history")
async def history(
    request: Request,
    who: Logged,
    instance: str,
    point: str,
    start: float | None = None,
    end: float | None = None,
) -> list[dict[str, Any]]:
    end = time.time() if end is None else end
    start = end - 86_400 if start is None else start
    return await services(request).history(who, instance, point, start, end)


@router.get("/daily/{instance}/{point:path}")
@action("history.daily")
async def daily(
    request: Request,
    who: Logged,
    instance: str,
    point: str,
    days: Annotated[int, Query(ge=1, le=400)] = 30,
) -> list[dict[str, Any]]:
    """Each day's lowest, mean and highest good value, in the house's time zone: kept
    for as long as the history's aggregates are, 400 days by default."""
    return await services(request).daily(who, instance, point, days)


# --- settings ----------------------------------------------------------------------------


@router.get("/settings/location")
@action("location.read")
async def location(request: Request, who: Logged) -> dict[str, Any] | None:
    found = await services(request).location(who)
    return None if found is None else found.model_dump(mode="json")


@router.put("/settings/location")
@action("location.write")
async def set_location(body: dict[str, Any], request: Request, who: Logged) -> dict[str, Any]:
    return (await services(request).set_location(who, body)).model_dump(mode="json")


@router.get("/plugins")
@action("plugins.read")
async def plugins(request: Request, who: Logged) -> dict[str, Any]:
    found = await services(request).plugins(who)
    return {id: p.model_dump(mode="json") for id, p in found.items()}


@router.put("/plugins/{id}/pump")
@action("pump.write")
async def set_pump(id: str, body: dict[str, Any], request: Request, who: Logged) -> dict[str, Any]:
    return (await services(request).set_pump(who, id, body)).model_dump(mode="json")


@router.get("/secrets")
@action("secrets.read")
async def secret_names(request: Request, who: Logged) -> list[str]:
    """The names of the secrets entered; their values are never given out."""
    return await services(request).secret_names(who)


@router.put("/secrets/{name}")
@action("secret.write")
async def set_secret(name: str, body: Secret, request: Request, who: Logged) -> dict[str, str]:
    await services(request).set_secret(who, name, body.value)
    return {"status": "ok"}


@router.get("/nibe/logset")
@action("logset")
async def logset(model: str, request: Request, who: Logged) -> Response:
    data = services(request).logset(who, model)
    return Response(
        data,
        media_type="application/octet-stream",
        headers={"content-disposition": 'attachment; filename="LOG.SET"'},
    )


# --- users and groups --------------------------------------------------------------------


@router.get("/users")
@action("users.read")
async def users(request: Request, who: Logged) -> list[dict[str, Any]]:
    return [
        {
            "name": u.name,
            "groups": list(u.groups),
            "rights": sorted(u.permissions),
            "disabled": u.disabled,
        }
        for u in await services(request).users(who)
    ]


@router.post("/users", status_code=201)
@action("user.create")
async def create_user(body: NewUser, request: Request, who: Logged) -> dict[str, str]:
    user = await services(request).create_user(who, body.name, body.password, body.groups)
    return {"user": user.name}


@router.put("/users/{name}/groups")
@action("user.groups")
async def set_user_groups(name: str, body: Groups, request: Request, who: Logged) -> dict[str, str]:
    await services(request).set_user_groups(who, name, body.groups)
    return {"status": "ok"}


@router.put("/users/{name}/password")
@action("user.password")
async def set_password(name: str, body: Password, request: Request, who: Logged) -> dict[str, str]:
    await services(request).set_user_password(
        who, name, body.password, end_sessions=body.end_sessions
    )
    return {"status": "ok"}


@router.delete("/users/{name}")
@action("user.delete")
async def delete_user(name: str, request: Request, who: Logged) -> dict[str, str]:
    await services(request).delete_user(who, name)
    return {"status": "ok"}


@router.get("/groups")
@action("groups.read")
async def groups(request: Request, who: Logged) -> dict[str, list[str]]:
    return {g: sorted(p) for g, p in (await services(request).groups(who)).items()}


@router.put("/groups/{name}")
@action("group.write")
async def set_group(name: str, body: Rights, request: Request, who: Logged) -> dict[str, str]:
    await services(request).set_group(who, name, body.permissions)
    return {"status": "ok"}


@router.delete("/groups/{name}")
@action("group.delete")
async def delete_group(name: str, request: Request, who: Logged) -> dict[str, str]:
    await services(request).delete_group(who, name)
    return {"status": "ok"}


@router.get("/rights")
@action("rights.read")
async def rights(who: Logged) -> dict[str, str]:
    """Every right there is, and what it allows."""
    return operations.rights()


# --- one's own language and formats ----------------------------------------------------


@router.get("/account/preferences")
@action("preferences.read")
async def preferences(request: Request, who: Logged) -> dict[str, Any]:
    return services(request).preferences(who).model_dump(exclude_none=True)


@router.put("/account/preferences")
@action("preferences.write")
async def set_preferences(body: dict[str, Any], request: Request, who: Logged) -> dict[str, Any]:
    """Language (en, sv, de), region (a country code: SE), and overrides: dates (iso),
    clock (24, 12), decimal (point, comma). A field left out takes the browser's."""
    chosen = await services(request).set_preferences(who, body)
    return chosen.model_dump(exclude_none=True)


# --- tokens and sessions -----------------------------------------------------------------


@router.get("/tokens")
@action("tokens.read")
async def tokens(request: Request, who: Logged) -> list[dict[str, Any]]:
    return [
        {
            "id": t.id,
            "name": t.name,
            "permissions": list(t.permissions),
            "created": t.created,
            "expires": t.expires,
            "last_used": t.last_used,
        }
        for t in await services(request).tokens(who)
    ]


@router.post("/tokens", status_code=201)
@action("token.create")
async def create_token(body: NewToken, request: Request, who: Logged) -> dict[str, str]:
    """The token is in this answer only; it can't be shown again."""
    raw = await services(request).create_token(who, body.name, body.permissions, body.days)
    return {"token": raw}


@router.delete("/tokens/{token_id}")
@action("token.revoke")
async def revoke_token(token_id: int, request: Request, who: Logged) -> dict[str, str]:
    await services(request).revoke_token(who, token_id)
    return {"status": "ok"}


@router.get("/sessions")
@action("sessions.read")
async def sessions(
    request: Request, who: Logged, user: Annotated[str | None, Query()] = None
) -> list[dict[str, Any]]:
    return [
        {
            "id": s.id,
            "created": s.created,
            "last_seen": s.last_seen,
            "from": s.source,
            "agent": s.agent,
            "display": s.display,
        }
        for s in await services(request).sessions(who, user)
    ]


# --- wall displays -----------------------------------------------------------------------


@router.get("/displays")
@action("wall_displays.read")
async def displays(request: Request, who: Logged) -> list[dict[str, Any]]:
    return [
        {
            "id": d.id,
            "name": d.name,
            "permissions": list(d.permissions),
            "created": d.created,
            "link_expires": d.link_expires,
            "opened": d.opened,
            "last_seen": d.last_seen,
        }
        for d in await services(request).displays(who)
    ]


@router.post("/displays", status_code=201)
@action("wall_display.create")
async def create_display(body: NewDisplay, request: Request, who: Logged) -> dict[str, str]:
    """The link is in this answer only. Opened on the display's browser within a day, it
    logs that browser in for good, with the display's rights; then it is used up."""
    raw = await services(request).create_display(who, body.name, body.permissions)
    return {"link": display_link(request, raw)}


@router.delete("/displays/{display_id}")
@action("wall_display.revoke")
async def revoke_display(display_id: int, request: Request, who: Logged) -> dict[str, str]:
    await services(request).revoke_display(who, display_id)
    return {"status": "ok"}


@router.post("/displays/open", dependencies=[Anonymous])
@action("wall_display.open")
async def open_display(body: OpenDisplay, request: Request, response: Response) -> dict[str, str]:
    """Open a display with its link's secret: a session that doesn't end by itself. The
    answer carries its CSRF token."""
    session_hash = await start_display_session(request, response, body.link.rsplit("/", 1)[-1])
    return {"csrf": csrf_token(request.app.state.csrf_key, session_hash)}


@router.delete("/sessions/{user}/{session_id}")
@action("session.end")
async def end_session(user: str, session_id: str, request: Request, who: Logged) -> dict[str, str]:
    await services(request).end_session(who, user, session_id)
    return {"status": "ok"}


# --- the audit log -----------------------------------------------------------------------


@router.get("/audit")
@action("audit.read")
async def audit(
    request: Request, who: Logged, limit: Annotated[int, Query(ge=1, le=2000)] = 200
) -> list[dict[str, Any]]:
    return await services(request).audit_entries(who, limit)
