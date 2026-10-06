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


async def end_browser_session(request: Request, response: Response, caller: Caller) -> None:
    raw = request.cookies.get(SESSION_COOKIE)
    if raw and caller.session is not None:
        await request.app.state.services.accounts.end_session(raw)
    response.delete_cookie(SESSION_COOKIE, path="/")
