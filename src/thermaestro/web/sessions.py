"""Logging a browser in and out: the session cookie."""

from fastapi import Request, Response

from ..auth.accounts import SESSION_MAX_S, digest
from .operations import Caller
from .security import PRE_SESSION_COOKIE, SESSION_COOKIE, client_address


async def start_browser_session(
    request: Request, response: Response, name: str, password: str
) -> str:
    """Check the password, start a new session and set its cookie; the session's hash,
    to bind the CSRF token to."""
    accounts = request.app.state.services.accounts
    source = client_address(request)
    user = await accounts.authenticate(name, password, source=source)
    raw = await accounts.start_session(user, source=source, agent=request.headers.get("user-agent"))
    response.set_cookie(
        SESSION_COOKIE,
        raw,
        max_age=int(SESSION_MAX_S),
        path="/",
        httponly=True,
        samesite="lax",
        secure=request.url.scheme == "https",
    )
    response.delete_cookie(PRE_SESSION_COOKIE, path="/")
    return digest(raw)


def display_link(request: Request, raw: str) -> str:
    """Where a wall display's link opens it: this site, as the request reached it."""
    return str(request.base_url).rstrip("/") + f"/wall-display/{raw}"


DISPLAY_COOKIE_S = 400 * 86_400
"""A wall display's cookie: as long as browsers keep one (400 days), and set again with
each page it loads, so it never runs out. The server decides when its session ends."""


def keep_display_cookie(request: Request, response: Response) -> None:
    """Set a wall display's cookie again, with its full lifetime."""
    raw = request.cookies.get(SESSION_COOKIE)
    if raw:
        response.set_cookie(
            SESSION_COOKIE,
            raw,
            max_age=DISPLAY_COOKIE_S,
            path="/",
            httponly=True,
            samesite="lax",
            secure=request.url.scheme == "https",
        )


async def start_display_session(request: Request, response: Response, raw: str) -> str:
    """Open a wall display with its link's secret, and set the session's cookie; the
    session's hash, to bind the CSRF token to."""
    accounts = request.app.state.services.accounts
    session = await accounts.open_display(
        raw, source=client_address(request), agent=request.headers.get("user-agent")
    )
    response.set_cookie(
        SESSION_COOKIE,
        session,
        max_age=DISPLAY_COOKIE_S,
        path="/",
        httponly=True,
        samesite="lax",
        secure=request.url.scheme == "https",
    )
    response.delete_cookie(PRE_SESSION_COOKIE, path="/")
    return digest(session)


async def end_browser_session(request: Request, response: Response, caller: Caller) -> None:
    raw = request.cookies.get(SESSION_COOKIE)
    if raw and caller.session is not None:
        await request.app.state.services.accounts.end_session(raw)
    response.delete_cookie(SESSION_COOKIE, path="/")
