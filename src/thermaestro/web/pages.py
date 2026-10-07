"""The HTML pages. Each form posts to an endpoint that calls the same operation as its
API counterpart, then redirects (or shows the page again with what went wrong)."""

import time
import zoneinfo
from functools import cache
from typing import Annotated, Any
from urllib.parse import quote

import jinja2
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from ..auth import PERMISSIONS, AccountError, LoginFailed
from . import i18n, labels
from .app import action, anonymous, caller, identify, local_path, services
from .operations import Caller, NeedsConfirmation
from .security import (
    PRE_SESSION_COOKIE,
    client_address,
    csrf_token,
    new_pre_session,
)
from .sessions import end_browser_session, start_browser_session

router = APIRouter(default_response_class=HTMLResponse)

Logged = Annotated[Caller, Depends(caller)]
Anyone = Annotated[Caller | None, Depends(anonymous)]
Text = Annotated[str, Form()]
Choices = Annotated[list[str] | None, Form()]


def _environment() -> jinja2.Environment:
    env = jinja2.Environment(
        loader=jinja2.PackageLoader(__package__ or "thermaestro.web", "templates"),
        autoescape=True,
        extensions=["jinja2.ext.i18n"],
        undefined=jinja2.StrictUndefined,
    )
    env.install_gettext_callables(  # type: ignore[attr-defined]
        lambda s: i18n.translations(i18n.current.get()).gettext(s),
        lambda s, p, n: i18n.translations(i18n.current.get()).ngettext(s, p, n),
        newstyle=True,
    )
    env.filters["num"] = i18n.number
    env.filters["when"] = i18n.when
    env.filters["unit"] = i18n.unit
    env.filters["value_label"] = labels.value
    env.globals["quality_color"] = labels.quality_color
    env.filters["quantity"] = labels.quantity
    env.filters["own_device"] = labels.own_device
    return env


templates = Jinja2Templates(env=_environment())


def render(
    request: Request,
    template: str,
    who: Caller | None,
    status_code: int = 200,
    **context: Any,
) -> Response:
    """A page, with what every page needs: who is logged in, the CSRF token for its forms,
    and the language."""
    key = request.app.state.csrf_key
    pre = None
    if who is not None and who.session is not None:
        token = csrf_token(key, who.session.hash)
    else:
        pre = request.cookies.get(PRE_SESSION_COOKIE) or new_pre_session()
        token = csrf_token(key, pre)
    response = templates.TemplateResponse(
        request,
        template,
        {
            "who": who,
            "csrf": token,
            "lang": i18n.current.get(),
            "languages": i18n.NAMES,
            "path": request.url.path,
            "can": (lambda right: who is not None and who.principal.allows(right)),
            "error": None,
            "notice": None,
            **context,
        },
        status_code=status_code,
    )
    if pre is not None and request.cookies.get(PRE_SESSION_COOKIE) != pre:
        response.set_cookie(
            PRE_SESSION_COOKIE,
            pre,
            path="/",
            httponly=True,
            samesite="lax",
            secure=request.url.scheme == "https",
        )
    response.headers["cache-control"] = "no-store"
    return response


def back(path: str) -> RedirectResponse:
    return RedirectResponse(path, status_code=303)


def _message(e: AccountError) -> str:
    """A refusal in the page's language, where the catalog has it."""
    return i18n._(str(e))


# --- logging in --------------------------------------------------------------------------


@router.get("/login")
async def login_page(request: Request, next: str = "/") -> Response:
    who = await identify(request)
    if who is not None and who.session is not None:
        return back(local_path(next))
    has_admin = await services(request).accounts.has_admin()
    return render(request, "login.html", None, next=local_path(next), has_admin=has_admin)


@router.post("/login")
@action("login")
async def login(
    request: Request, _: Anyone, name: Text, password: Text, next: Text = "/"
) -> Response:
    response = back(local_path(next))
    try:
        await start_browser_session(request, response, name, password)
    except LoginFailed as e:
        has_admin = await services(request).accounts.has_admin()
        return render(
            request,
            "login.html",
            None,
            status_code=400,
            next=local_path(next),
            has_admin=has_admin,
            error=_message(e),
            name=name,
        )
    return response


@router.post("/logout")
@action("logout")
async def logout(request: Request, who: Logged) -> Response:
    response = back("/login")
    await end_browser_session(request, response, who)
    return response


@router.get("/setup")
async def setup_page(request: Request) -> Response:
    if await services(request).accounts.has_admin():
        return back("/login")
    return render(request, "setup.html", None, name="")


@router.post("/setup")
@action("setup")
async def setup(
    request: Request, _: Anyone, code: Text, name: Text, password: Text, again: Text
) -> Response:
    s = services(request)
    try:
        if password != again:
            raise AccountError("the two passwords differ")
        await s.accounts.create_first_admin(
            s.setup, code, name, password, source=client_address(request)
        )
    except AccountError as e:
        return render(request, "setup.html", None, status_code=400, error=_message(e), name=name)
    response = back("/")
    await start_browser_session(request, response, name, password)
    return response


@router.get("/confirm")
async def confirm_page(request: Request, who: Logged, next: str = "/") -> Response:
    return render(request, "confirm.html", who, next=local_path(next))


@router.post("/confirm")
@action("confirm")
async def confirm(request: Request, who: Logged, password: Text, next: Text = "/") -> Response:
    if who.session is not None:
        try:
            await services(request).accounts.confirm(
                who.session, password, source=client_address(request)
            )
        except AccountError as e:
            return render(
                request,
                "confirm.html",
                who,
                status_code=400,
                next=local_path(next),
                error=_message(e),
            )
    return back(local_path(next))


@router.get("/lang/{code}")
async def language(code: str, next: str = "/") -> Response:
    """Remembers the language chosen in this browser."""
    response = back(local_path(next))
    if code in i18n.LANGUAGES:
        response.set_cookie("lang", code, max_age=400 * 86_400, path="/", samesite="lax")
    return response


# --- status ------------------------------------------------------------------------------


@router.get("/")
async def status_page(request: Request, who: Logged) -> Response:
    s = services(request)
    return render(request, "status.html", who, instances=s.status(who), site=s.site(who))


@router.get("/status/values")
async def status_values(request: Request, who: Logged) -> Response:
    """The values table alone, which the status page reloads every few seconds."""
    s = services(request)
    return render(request, "values.html", who, instances=s.status(who), site=s.site(who))


@router.get("/points/{instance}/{point:path}")
async def point_page(request: Request, who: Logged, instance: str, point: str) -> Response:
    who.principal.require("points.read")
    s = services(request)
    source = f"/api/v1/history/{quote(instance, safe='')}/{quote(point, safe='/')}"
    node = point.rpartition("/")[0] or None
    return render(
        request,
        "point.html",
        who,
        instance=instance,
        point=point,
        label=s.point_label(who, instance, point),
        built_in=s.built_in_label(instance, point),
        node=node,
        digits=s.point_digits(instance, point),
        node_built_in=s.node_label(who, instance, node, built_in=True) if node else "",
        names=await s.names(who),
        source=source,
        decimal=i18n.decimal_symbol(),
        zone=i18n.zone_name(),
        formats=i18n.formats.get(),
    )


# --- settings ----------------------------------------------------------------------------

REGIONS = (
    "Europe",
    "Africa",
    "America",
    "Antarctica",
    "Arctic",
    "Asia",
    "Atlantic",
    "Australia",
    "Indian",
    "Pacific",
)


@cache
def time_zones() -> dict[str, list[tuple[str, str]]]:
    """The time zones to choose from, by region, as (name, label): the system's IANA
    zones, without the old aliases (`EST`, `Etc/GMT+1`, `US/Eastern`)."""
    grouped: dict[str, list[tuple[str, str]]] = {r: [] for r in REGIONS}
    for name in zoneinfo.available_timezones():
        region, _, place = name.partition("/")
        if region in grouped and place:
            grouped[region].append((name, place.replace("_", " ").replace("/", " / ")))
    return {r: sorted(z, key=lambda zone: zone[1]) for r, z in grouped.items() if z}


async def _settings(
    request: Request, who: Caller, status_code: int = 200, **extra: Any
) -> Response:
    s = services(request)
    location = await s.location(who)
    plugins = await s.plugins(who)
    pumps = {id: p for id, p in plugins.items() if p.plugin == "nibe"}
    from ..nibe.maps import load

    models = sorted(load("bus").models)
    return render(
        request,
        "settings.html",
        who,
        status_code=status_code,
        location=location,
        zones=time_zones(),
        pumps=pumps,
        models=models,
        fingerprint=s.fingerprint,
        **extra,
    )


@router.get("/settings")
async def settings_page(request: Request, who: Logged) -> Response:
    return await _settings(request, who)


@router.post("/settings/location")
@action("location.write")
async def set_location(
    request: Request, who: Logged, latitude: Text, longitude: Text, timezone: Text
) -> Response:
    body = {"latitude": latitude, "longitude": longitude, "timezone": timezone.strip()}
    try:
        await services(request).set_location(who, body)
    except AccountError as e:
        return await _settings(request, who, 400, error=_message(e))
    return back("/settings")


@router.post("/settings/pump")
@action("pump.write")
async def set_pump(
    request: Request,
    who: Logged,
    id: Text,
    host: Text,
    protocol: Text,
    read_port: Text = "9999",
    write_port: Text = "10000",
    control_port: Text = "10090",
    local_port: Text = "0",
    model: Text = "",
    psk: Text = "",
) -> Response:
    s = services(request)
    id = id.strip() or "pump"
    key_name = f"{id.lower()}.psk"
    existing = await s.plugins(who)
    old = existing.get(id)
    had_key = old is not None and old.settings.get("psk") is not None
    body: dict[str, Any] = {
        "host": host.strip(),
        "protocol": protocol,
        "read_port": read_port,
        "write_port": write_port,
        "control_port": control_port,
        "local_port": local_port,
        "model": model.strip() or None,
    }
    if psk or had_key or protocol == "thermaestro-gw":
        body["psk"] = key_name
    try:
        if psk:
            await s.set_secret(who, key_name, psk.strip())
        await s.set_pump(who, id, body)
    except NeedsConfirmation:
        raise
    except AccountError as e:
        return await _settings(request, who, 400, error=_message(e))
    return back("/settings")


# --- users and groups --------------------------------------------------------------------


async def _users(request: Request, who: Caller, status_code: int = 200, **extra: Any) -> Response:
    s = services(request)
    return render(
        request,
        "users.html",
        who,
        status_code=status_code,
        users=await s.users(who),
        groups=await s.groups(who),
        rights=PERMISSIONS,
        **extra,
    )


@router.get("/users")
async def users_page(request: Request, who: Logged) -> Response:
    return await _users(request, who)


@router.post("/users")
@action("user.create")
async def create_user(
    request: Request, who: Logged, name: Text, password: Text, groups: Choices = None
) -> Response:
    try:
        await services(request).create_user(who, name.strip(), password, groups or [])
    except NeedsConfirmation:
        raise
    except AccountError as e:
        return await _users(request, who, 400, error=_message(e))
    return back("/users")


@router.post("/users/{name}/groups")
@action("user.groups")
async def set_user_groups(
    request: Request, who: Logged, name: str, groups: Choices = None
) -> Response:
    try:
        await services(request).set_user_groups(who, name, groups or [])
    except NeedsConfirmation:
        raise
    except AccountError as e:
        return await _users(request, who, 400, error=_message(e))
    return back("/users")


@router.post("/users/{name}/password")
@action("user.password")
async def set_user_password(
    request: Request, who: Logged, name: str, password: Text, again: Text
) -> Response:
    own = name.lower() == who.principal.user.name.lower()
    try:
        if password != again:
            raise AccountError("the two passwords differ")
        # One's own password keeps this session; everyone else's sessions end.
        await services(request).set_user_password(who, name, password, end_sessions=not own)
    except NeedsConfirmation:
        raise
    except AccountError as e:
        if own:
            return await _account(request, who, 400, error=_message(e))
        return await _users(request, who, 400, error=_message(e))
    return back("/account" if own else "/users")


@router.post("/users/{name}/delete")
@action("user.delete")
async def delete_user(request: Request, who: Logged, name: str) -> Response:
    try:
        await services(request).delete_user(who, name)
    except NeedsConfirmation:
        raise
    except AccountError as e:
        return await _users(request, who, 400, error=_message(e))
    return back("/users")


@router.post("/groups")
@action("group.write")
async def set_group(request: Request, who: Logged, name: Text, rights: Choices = None) -> Response:
    try:
        await services(request).set_group(who, name.strip(), rights or [])
    except NeedsConfirmation:
        raise
    except AccountError as e:
        return await _users(request, who, 400, error=_message(e))
    return back("/users")


@router.post("/groups/{name}/delete")
@action("group.delete")
async def delete_group(request: Request, who: Logged, name: str) -> Response:
    try:
        await services(request).delete_group(who, name)
    except NeedsConfirmation:
        raise
    except AccountError as e:
        return await _users(request, who, 400, error=_message(e))
    return back("/users")


# --- one's own account -------------------------------------------------------------------


async def _account(request: Request, who: Caller, status_code: int = 200, **extra: Any) -> Response:
    s = services(request)
    tokens = await s.tokens(who) if who.principal.allows("tokens.own") else []
    own_rights = {r: d for r, d in PERMISSIONS.items() if who.principal.allows(r)}
    language = i18n.current.get()
    return render(
        request,
        "account.html",
        who,
        status_code=status_code,
        preferences=who.principal.user.preferences,
        regions=i18n.regions(language),
        example=f"{i18n.when(time.time())}   {i18n.number(-1234.5)}",
        tokens=tokens,
        sessions=await s.sessions(who),
        current=who.session.id if who.session else None,
        rights=own_rights,
        **extra,
    )


@router.get("/account")
async def account_page(request: Request, who: Logged) -> Response:
    return await _account(request, who)


@router.post("/account/preferences")
@action("preferences.write")
async def set_preferences(
    request: Request,
    who: Logged,
    language: Text = "",
    region: Text = "",
    dates: Text = "",
    clock: Text = "",
    decimal: Text = "",
) -> Response:
    fields = {
        "language": language,
        "region": region,
        "dates": dates,
        "clock": clock,
        "decimal": decimal,
    }
    body = {k: v for k, v in fields.items() if v}
    try:
        await services(request).set_preferences(who, body)
    except AccountError as e:
        return await _account(request, who, 400, error=_message(e))
    return back("/account#formats")


@router.post("/account/tokens")
@action("token.create")
async def create_token(
    request: Request,
    who: Logged,
    name: Text,
    days: Text = "365",
    rights: Choices = None,
) -> Response:
    try:
        try:
            lasting = int(days)
        except ValueError:
            raise AccountError("a token lasts 1 to 3650 days") from None
        raw = await services(request).create_token(who, name.strip(), rights or [], lasting)
    except NeedsConfirmation:
        raise
    except AccountError as e:
        return await _account(request, who, 400, error=_message(e))
    # Shown once, on this page, and never again.
    return await _account(request, who, new_token=raw)


@router.post("/account/tokens/{token_id}/revoke")
@action("token.revoke")
async def revoke_token(request: Request, who: Logged, token_id: int) -> Response:
    try:
        await services(request).revoke_token(who, token_id)
    except AccountError as e:
        return await _account(request, who, 400, error=_message(e))
    return back("/account")


@router.post("/account/sessions/{session_id}/end")
@action("session.end")
async def end_session(request: Request, who: Logged, session_id: str) -> Response:
    try:
        await services(request).end_session(who, who.principal.user.name, session_id)
    except AccountError as e:
        return await _account(request, who, 400, error=_message(e))
    if who.session is not None and session_id == who.session.id:
        return back("/login")
    return back("/account")


# --- diagnostics and the audit log -------------------------------------------------------


@router.get("/diagnostics")
async def diagnostics_page(request: Request, who: Logged) -> Response:
    s = services(request)
    who.principal.require("settings.read")
    from ..nibe.maps import load

    return render(
        request,
        "diagnostics.html",
        who,
        instances=s.status(who),
        models=sorted(load("bus").models),
        fingerprint=s.fingerprint,
    )


@router.get("/audit")
async def audit_page(request: Request, who: Logged) -> Response:
    entries = await services(request).audit_entries(who, 300)
    return render(request, "audit.html", who, entries=entries)
