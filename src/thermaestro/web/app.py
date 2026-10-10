"""The web application: the HTML pages and the JSON API, over the same operations.

Every endpoint but the login and setup pages, the static files and `/health` needs a
logged-in user or a token. Unsafe requests from a browser need the CSRF token and must
come from this site's own origin.
"""

from collections.abc import Callable
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from starlette.staticfiles import StaticFiles

from ..auth import AccountError, Forbidden, Principal, User
from . import i18n
from .operations import Caller, NeedsConfirmation, NotFound, Services
from .security import (
    CSRF_FIELD,
    CSRF_HEADER,
    PRE_SESSION_COOKIE,
    SAFE_METHODS,
    SESSION_COOKIE,
    SecurityHeaders,
    client_address,
    csrf_matches,
    same_origin,
)

TELEMETRY_OFF = {
    "tracing": False,
    "metrics": False,
    "logs": False,
    "operation_spans": False,
    "auto_configure": False,
}
"""FastAPI would otherwise export traces wherever OTEL_* variables point: Thermaestro
sends nothing anywhere unasked."""


class NotLoggedIn(Exception):
    pass


class BadRequestOrigin(Exception):
    """An unsafe browser request without the CSRF token, or from another site."""


STEP_UP_FIELD = "step_up_password"
"""A form's field carrying the password entered again, with the form it was asked for."""


class StepUpRefused(Exception):
    """The password entered again, with a form, wasn't right."""

    def __init__(self, why: str) -> None:
        super().__init__(why)
        self.why = why


def action[F: Callable[..., Any]](name: str) -> Callable[[F], F]:
    """Names the operation an endpoint performs. Each one a page can do has an API
    endpoint with the same name; a test checks it."""

    def mark(endpoint: F) -> F:
        endpoint.__action__ = name  # type: ignore[attr-defined]
        return endpoint

    return mark


def services(request: Request) -> Services:
    found: Services = request.app.state.services
    return found


async def identify(request: Request) -> Caller | None:
    """Who sent the request: a bearer token, a session cookie, or nobody. A request with
    a token is never also taken as a session."""
    accounts = services(request).accounts
    source = client_address(request)
    authorization = request.headers.get("authorization", "")
    if authorization:
        scheme, _, raw = authorization.partition(" ")
        if scheme.lower() != "bearer":
            return None
        principal = await accounts.principal_for_token(raw.strip())
        if principal is None:
            return None
        _use_preferences(request, principal.user)
        return Caller(principal, None, source)
    cookie = request.cookies.get(SESSION_COOKIE)
    if not cookie:
        return None
    session = await accounts.session(cookie)
    if session is None:
        return None
    _use_preferences(request, session.user)
    return Caller(Principal(session.user), session, source)


def _use_preferences(request: Request, user: User) -> None:
    """The user's own language and formats for the rest of the request. The Language
    middleware set the browser's, and puts its own back when the request is done."""
    language, chosen = i18n.resolve(
        user.preferences,
        request.cookies.get("lang"),
        request.headers.get("accept-language"),
        services(request).zone,
    )
    i18n.current.set(language)
    i18n.formats.set(chosen)


async def check_csrf(request: Request, caller: Caller | None) -> None:
    """For an unsafe request that isn't a token's: the origin, then the token, from the
    header or the form."""
    if request.method in SAFE_METHODS or (caller is not None and caller.session is None):
        return
    if not same_origin(request):
        raise BadRequestOrigin
    if caller is not None and caller.session is not None:
        binding = caller.session.hash
    else:
        binding = request.cookies.get(PRE_SESSION_COOKIE, "")
        if not binding:
            raise BadRequestOrigin
    token = request.headers.get(CSRF_HEADER)
    if token is None and request.headers.get("content-type", "").startswith(
        ("application/x-www-form-urlencoded", "multipart/form-data")
    ):
        form = await request.form()
        value = form.get(CSRF_FIELD)
        token = value if isinstance(value, str) else None
    if not csrf_matches(request.app.state.csrf_key, binding, token):
        raise BadRequestOrigin


async def caller(request: Request) -> Caller:
    """The dependency of every endpoint that needs someone logged in."""
    found = await identify(request)
    if found is None:
        raise NotLoggedIn
    await check_csrf(request, found)
    await _step_up(request, found)
    return found


async def _step_up(request: Request, found: Caller) -> None:
    """A form sent again with the password it asked for: the password first, then the
    form's own action, as if it had never been interrupted."""
    if request.method != "POST" or found.session is None or not _is_form(request):
        return
    password = (await request.form()).get(STEP_UP_FIELD)
    if not isinstance(password, str) or not password:
        return
    accounts = services(request).accounts
    try:
        await accounts.confirm(found.session, password, source=client_address(request))
    except AccountError as e:
        raise StepUpRefused(i18n._(str(e))) from None
    # The session as it is now, confirmed, for the action that follows.
    fresh = await accounts.session(request.cookies.get(SESSION_COOKIE, ""))
    if fresh is not None:
        found.session = fresh


def _is_form(request: Request) -> bool:
    return request.headers.get("content-type", "").startswith(
        ("application/x-www-form-urlencoded", "multipart/form-data")
    )


async def anonymous(request: Request) -> Caller | None:
    """The dependency of the login and setup forms: CSRF-checked, login optional."""
    found = await identify(request)
    await check_csrf(request, found)
    return found


async def anonymous_api(request: Request) -> None:
    """The dependency of the API's login and setup. They take JSON only, which a form on
    another site can't send, and a script has no page to take a token from; a browser
    names the page's origin on every cross-site POST, so another site's is refused."""
    has_origin = "origin" in request.headers or "referer" in request.headers
    if has_origin and not same_origin(request):
        raise BadRequestOrigin


def local_path(target: str | None, default: str = "/") -> str:
    """A redirect target from a form, only if it stays on this site."""
    if not target or not target.startswith("/") or target.startswith("//") or "\\" in target:
        return default
    return target


class Language:
    """Sets the request's language and formats from the browser: the `lang` cookie or
    Accept-Language. A logged-in user's own choices replace them once known."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        headers = dict(scope.get("headers", []))
        cookies = headers.get(b"cookie", b"").decode("latin-1")
        chosen = None
        for part in cookies.split(";"):
            name, _, value = part.strip().partition("=")
            if name == "lang":
                chosen = value
        accept = headers.get(b"accept-language", b"").decode("latin-1")
        zone = scope["app"].state.services.zone
        language, formats = i18n.resolve(None, chosen, accept, zone)
        with i18n.using(language, formats):
            await self.app(scope, receive, send)


def create_app(services: Services, csrf_key: bytes) -> FastAPI:
    # The API's description is served to logged-in users only (/api/v1/openapi.json);
    # FastAPI's own docs pages load from a CDN with inline scripts, so they're off.
    app = FastAPI(
        title="Thermaestro",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        telemetry=TELEMETRY_OFF,  # type: ignore[arg-type]
    )
    app.state.services = services
    app.state.csrf_key = csrf_key
    app.add_middleware(Language)
    app.add_middleware(SecurityHeaders)

    from importlib import resources

    static = resources.files(__package__).joinpath("static")
    app.mount("/static", StaticFiles(directory=str(static)), name="static")

    @app.exception_handler(NotLoggedIn)
    async def not_logged_in(request: Request, _: Exception) -> Response:
        if request.url.path.startswith("/api/"):
            return JSONResponse({"error": "log in, or send a token"}, status_code=401)
        target = request.url.path + (f"?{request.url.query}" if request.url.query else "")
        response: Response
        if "hx-request" in request.headers:
            # A part of a page htmx reloads: the whole page goes to the login, rather
            # than the login form into the part.
            response = Response(status_code=401, headers={"HX-Redirect": "/login"})
        else:
            response = RedirectResponse(f"/login?next={_quote(target)}", status_code=303)
        if SESSION_COOKIE in request.cookies:
            response.delete_cookie(SESSION_COOKIE, path="/")
        return response

    @app.exception_handler(BadRequestOrigin)
    async def bad_origin(request: Request, _: Exception) -> Response:
        message = i18n._("The form had expired, or came from another site. Please try again.")
        if request.url.path.startswith("/api/"):
            return JSONResponse({"error": "missing or wrong CSRF token, or another origin"}, 403)
        return Response(message, status_code=403, media_type="text/plain; charset=utf-8")

    @app.exception_handler(NeedsConfirmation)
    async def needs_confirmation(request: Request, e: Exception) -> Response:
        if request.url.path.startswith("/api/"):
            return JSONResponse({"error": str(e), "confirm": True}, status_code=403)
        if request.method == "POST" and _is_form(request):
            return await _ask_again(request, None)
        referer = request.headers.get("referer", "")
        back = "/" + referer.split("/", 3)[3] if referer.count("/") >= 3 else "/"
        return RedirectResponse(f"/confirm?next={_quote(local_path(back))}", status_code=303)

    @app.exception_handler(StepUpRefused)
    async def step_up_refused(request: Request, e: Exception) -> Response:
        return await _ask_again(request, e.why if isinstance(e, StepUpRefused) else str(e))

    @app.exception_handler(Forbidden)
    async def forbidden(request: Request, e: Exception) -> Response:
        if request.url.path.startswith("/api/"):
            return JSONResponse({"error": str(e)}, status_code=403)
        return Response(
            i18n._("You don't have the right to do that."),
            status_code=403,
            media_type="text/plain; charset=utf-8",
        )

    @app.exception_handler(NotFound)
    async def not_found(request: Request, e: Exception) -> Response:
        return JSONResponse({"error": str(e)}, status_code=404)

    @app.exception_handler(AccountError)
    async def refused(request: Request, e: Exception) -> Response:
        return JSONResponse({"error": str(e)}, status_code=400)

    @app.get("/health", include_in_schema=False)
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    from . import (
        api,
        control_api,
        pages,
        price_api,
        price_pages,
        sensor_api,
        sensor_pages,
        setup_pages,
        weather_api,
        weather_pages,
    )

    app.include_router(setup_pages.router)
    app.include_router(api.router)
    app.include_router(control_api.router)
    app.include_router(price_api.router)
    app.include_router(price_pages.router)
    app.include_router(sensor_api.router)
    app.include_router(sensor_pages.router)
    app.include_router(weather_api.router)
    app.include_router(weather_pages.router)
    app.include_router(pages.router)

    @app.get("/api/v1/openapi.json", include_in_schema=False)
    async def openapi(request: Request) -> JSONResponse:
        if await identify(request) is None:
            raise NotLoggedIn
        return JSONResponse(app.openapi())

    return app


async def _ask_again(request: Request, error: str | None) -> Response:
    """The password page, carrying the form that asked for it: on submit, the form goes to
    its own action again, with the password. What was typed into it stays in this page
    only, which isn't kept (no-store)."""
    from .pages import render

    form = await request.form()
    fields = [
        (k, v)
        for k, v in form.multi_items()
        if k not in (CSRF_FIELD, STEP_UP_FIELD) and isinstance(v, str)
    ]
    action = request.url.path + (f"?{request.url.query}" if request.url.query else "")
    return render(
        request,
        "confirm.html",
        await identify(request),
        status_code=400 if error else 200,
        next="/",
        carry={"action": action, "fields": fields, "password": STEP_UP_FIELD},
        error=error,
    )


def _quote(path: str) -> str:
    from urllib.parse import quote

    return quote(path, safe="/")
