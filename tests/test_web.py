import json
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

import httpx
import pytest
from argon2 import PasswordHasher
from capfake import FakePump
from fastapi import FastAPI
from fastapi.routing import APIRoute

from thermaestro.auth import Accounts, AddressLimiter, SetupCode
from thermaestro.core import AuditLog
from thermaestro.core.host import Instance, PluginHost, State
from thermaestro.core.values import Values
from thermaestro.store import Database, Plugin, SecretStore
from thermaestro.web import (
    Services,
    api,
    create_app,
    pages,
    price_api,
    price_pages,
    sensor_api,
    sensor_pages,
    weather_api,
    weather_pages,
)
from thermaestro.web.app import TELEMETRY_OFF

ADMIN_PASSWORD = "correct horse battery staple"
FAST = PasswordHasher(time_cost=1, memory_cost=8, parallelism=1)
ORIGIN = "http://testserver"
KEY = b"k" * 32


class Clock:
    def __init__(self) -> None:
        self.now = 2_000_000_000.0

    def __call__(self) -> float:
        return self.now


class Site:
    def __init__(self, app: FastAPI, services: Services, clock: Clock, state: Path) -> None:
        self.app = app
        self.services = services
        self.clock = clock
        self.state = state

    def client(self, **headers: str) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app),
            base_url=ORIGIN,
            headers={"origin": ORIGIN, **headers},
        )


@pytest.fixture
async def site(tmp_path: Path) -> AsyncIterator[Site]:
    clock = Clock()
    async with await Database.open(tmp_path / "t.db") as db:
        audit = AuditLog(tmp_path / "audit")
        values = Values(db)
        secrets = SecretStore(tmp_path / "secrets.json")
        accounts = Accounts(db, audit, hasher=FAST, clock=clock, limiter=AddressLimiter(tries=1000))
        host = PluginHost(db=db, secrets=secrets, values=values, audit=audit, factories={})
        services = Services(
            accounts=accounts,
            db=db,
            values=values,
            host=host,
            audit=audit,
            secrets=secrets,
            setup=SetupCode(tmp_path / "setup-code", clock),
            fingerprint="AB:CD",
        )
        yield Site(create_app(services, KEY), services, clock, tmp_path)


def routes(app: FastAPI) -> list[APIRoute]:
    """Every endpoint: the app's own, and its routers' (FastAPI keeps an included router
    as one entry of the app's routes)."""
    included = [
        *api.router.routes,
        *pages.router.routes,
        *sensor_api.router.routes,
        *sensor_pages.router.routes,
        *price_api.router.routes,
        *price_pages.router.routes,
        *weather_api.router.routes,
        *weather_pages.router.routes,
    ]
    return [r for r in (*app.routes, *included) if isinstance(r, APIRoute)]


def csrf_of(html: str) -> str:
    match = re.search(r'name="csrf" value="([0-9a-f]+)"', html)
    assert match, "the page has no CSRF token"
    return match.group(1)


@asynccontextmanager
async def admin(site: Site) -> AsyncIterator[httpx.AsyncClient]:
    """A client logged in as an administrator."""
    if not await site.services.accounts.has_admin():
        await site.services.accounts.create_user(
            "admin", ADMIN_PASSWORD, ["Administrators"], by="cli"
        )
    async with site.client() as client:
        page = await client.get("/login")
        answer = await client.post(
            "/login",
            data={"csrf": csrf_of(page.text), "name": "admin", "password": ADMIN_PASSWORD},
        )
        assert answer.status_code == 303, answer.text
        yield client


async def form(client: httpx.AsyncClient, page: str, action: str, **fields: Any) -> httpx.Response:
    """Post a form the way a browser would: with the token from the page it's on."""
    html = (await client.get(page)).text
    return await client.post(action, data={"csrf": csrf_of(html), **fields})


# --- what anyone sees --------------------------------------------------------------------


async def test_health_and_headers(site: Site) -> None:
    async with site.client() as client:
        answer = await client.get("/health")
    assert answer.json() == {"status": "ok"}
    csp = answer.headers["content-security-policy"]
    assert "default-src 'self'" in csp
    assert "unsafe-inline" not in csp
    assert "unsafe-eval" not in csp
    assert answer.headers["x-content-type-options"] == "nosniff"
    assert answer.headers["cross-origin-opener-policy"] == "same-origin"
    assert answer.headers["referrer-policy"] == "strict-origin-when-cross-origin"


async def test_everything_else_needs_a_login(site: Site) -> None:
    async with site.client() as client:
        checked = 0
        for route in routes(site.app):
            path = re.sub(r"\{[^}]+\}", "x", route.path)
            if path in (
                "/login",
                "/setup",
                "/health",
                "/lang/x",
                "/theme/x",
                "/api/v1/login",
                "/api/v1/setup",
            ):
                continue
            for method in route.methods or ():
                answer = await client.request(method, path)
                if path.startswith("/api/"):
                    assert answer.status_code == 401, (method, path)
                else:
                    assert answer.status_code == 303, (method, path)
                    assert answer.headers["location"].startswith("/login")
                checked += 1
        assert checked > 40
        assert (await client.get("/docs")).status_code == 404
        assert (await client.get("/openapi.json")).status_code == 404


async def test_the_api_description_is_for_logged_in_users(site: Site) -> None:
    async with admin(site) as client:
        answer = await client.get("/api/v1/openapi.json")
    assert answer.status_code == 200
    assert "/api/v1/status" in answer.json()["paths"]


def test_telemetry_is_off() -> None:
    assert set(TELEMETRY_OFF) == {
        "tracing",
        "metrics",
        "logs",
        "operation_spans",
        "auto_configure",
    }
    assert not any(TELEMETRY_OFF.values())


async def test_telemetry_ignores_the_environment(
    site: Site, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://192.0.2.99:4318")
    app = create_app(site.services, KEY)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=ORIGIN) as c:
        assert (await c.get("/health")).status_code == 200


# --- the first administrator -------------------------------------------------------------


async def test_first_run(site: Site) -> None:
    code = site.services.setup.issue()
    async with site.client() as client:
        login = await client.get("/login")
        assert "/setup" in login.text  # no administrator yet
        page = await client.get("/setup")
        token = csrf_of(page.text)
        fields = {"csrf": token, "name": "anna", "password": ADMIN_PASSWORD}
        wrong = await client.post(
            "/setup", data={**fields, "code": "AAAA", "again": ADMIN_PASSWORD}
        )
        assert wrong.status_code == 400
        assert "setup code" in wrong.text
        done = await client.post("/setup", data={**fields, "code": code, "again": ADMIN_PASSWORD})
        assert done.status_code == 303
        assert (await client.get("/")).status_code == 200  # and logged in
        again = await client.get("/setup")
        assert again.headers["location"] == "/setup/house"  # Setup's first topic
    assert site.services.setup.current() is None


# --- logging in --------------------------------------------------------------------------


async def test_wrong_user_and_wrong_password_look_the_same(site: Site) -> None:
    await site.services.accounts.create_user("anna", ADMIN_PASSWORD, by="cli")
    async with site.client() as client:
        page = await client.get("/login")
        token = csrf_of(page.text)
        unknown = await client.post(
            "/login", data={"csrf": token, "name": "nobody", "password": ADMIN_PASSWORD}
        )
        wrong = await client.post(
            "/login", data={"csrf": token, "name": "anna", "password": "wrong" * 4}
        )
    assert unknown.status_code == wrong.status_code == 400
    assert unknown.text.replace("nobody", "anna") == wrong.text


async def test_login_and_logout(site: Site) -> None:
    async with admin(site) as client:
        assert (await client.get("/")).status_code == 200
        logout = await form(client, "/", "/logout")
        assert logout.headers["location"] == "/login"
        assert (await client.get("/")).status_code == 303


async def test_redirects_stay_on_the_site(site: Site) -> None:
    await site.services.accounts.create_user("admin", ADMIN_PASSWORD, ["Administrators"], by="cli")
    for target in ("https://evil.example/", "//evil.example/", "/\\evil.example"):
        async with site.client() as client:
            page = await client.get("/login")
            answer = await client.post(
                "/login",
                data={
                    "csrf": csrf_of(page.text),
                    "name": "admin",
                    "password": ADMIN_PASSWORD,
                    "next": target,
                },
            )
            assert answer.headers["location"] == "/"


async def test_sessions_end_when_idle(site: Site) -> None:
    async with admin(site) as client:
        site.clock.now += 12 * 3600
        answer = await client.get("/")
    assert answer.status_code == 303


# --- CSRF and origin ---------------------------------------------------------------------


async def test_unsafe_requests_need_the_token_and_the_origin(site: Site) -> None:
    async with admin(site) as client:
        token = csrf_of((await client.get("/setup/house")).text)
        fields = {"latitude": "52.52", "longitude": "13.40", "timezone": "Europe/Berlin"}
        no_token = await client.post("/settings/location", data=fields)
        assert no_token.status_code == 403
        wrong = await client.post("/settings/location", data={**fields, "csrf": "0" * 64})
        assert wrong.status_code == 403
        other_site = await client.post(
            "/settings/location",
            data={**fields, "csrf": token},
            headers={"origin": "http://evil.example"},
        )
        assert other_site.status_code == 403
        no_origin = client.build_request(
            "POST", "/settings/location", data={**fields, "csrf": token}
        )
        del no_origin.headers["origin"]
        assert (await client.send(no_origin)).status_code == 403
        by_referer = client.build_request(
            "POST", "/settings/location", data={**fields, "csrf": token}
        )
        del by_referer.headers["origin"]
        by_referer.headers["referer"] = f"{ORIGIN}/settings"
        assert (await client.send(by_referer)).status_code == 303
        ok = await client.post("/settings/location", data={**fields, "csrf": token})
        assert ok.status_code == 303
        header = await client.put(
            "/api/v1/settings/location",
            json={"latitude": 52.5, "longitude": 13.4, "timezone": "Europe/Berlin"},
            headers={"x-csrf-token": token},
        )
        assert header.status_code == 200


async def test_the_login_form_needs_its_pre_session_token(site: Site) -> None:
    await site.services.accounts.create_user("admin", ADMIN_PASSWORD, ["Administrators"], by="cli")
    async with site.client() as client:
        answer = await client.post("/login", data={"name": "admin", "password": ADMIN_PASSWORD})
        assert answer.status_code == 403
    async with site.client() as other:
        token = csrf_of((await other.get("/login")).text)
    async with site.client() as client:
        await client.get("/login")  # its own pre-session cookie, not the other's
        answer = await client.post(
            "/login", data={"csrf": token, "name": "admin", "password": ADMIN_PASSWORD}
        )
        assert answer.status_code == 403


# --- tokens and the API ------------------------------------------------------------------


async def test_a_token_works_without_cookies_and_within_its_rights(site: Site) -> None:
    async with admin(site) as client:
        created = await form(
            client, "/account", "/account/tokens", name="script", days="30", rights="points.read"
        )
        assert created.status_code == 200
        raw = re.search(r"(thm_[A-Za-z0-9_-]+)", created.text)
        assert raw
        assert raw.group(1) not in (await client.get("/account")).text  # shown once
    async with site.client(authorization=f"Bearer {raw.group(1)}") as api:
        assert (await api.get("/api/v1/status")).status_code == 200
        assert (await api.get("/api/v1/users")).status_code == 403
        refused = await api.put(
            "/api/v1/settings/location",
            json={"latitude": 1, "longitude": 1, "timezone": "UTC"},
            headers={"origin": "http://evil.example"},  # no cookies: no CSRF either
        )
        assert refused.status_code == 403
        assert refused.json()["error"] == "not allowed: needs settings.write"
    async with site.client(authorization="Bearer thm_nonsense") as api:
        assert (await api.get("/api/v1/status")).status_code == 401


VIEWER_REFUSED = [
    ("GET", "/api/v1/settings/location"),
    ("GET", "/api/v1/plugins"),
    ("GET", "/api/v1/secrets"),
    ("GET", "/api/v1/users"),
    ("GET", "/api/v1/groups"),
    ("GET", "/api/v1/tokens"),
    ("GET", "/api/v1/audit"),
    ("GET", "/api/v1/nibe/logset?model=F1245"),
    ("PUT", "/api/v1/settings/location"),
    ("PUT", "/api/v1/plugins/pump/pump"),
    ("PUT", "/api/v1/secrets/pump.psk"),
    ("POST", "/api/v1/users"),
    ("PUT", "/api/v1/users/admin/groups"),
    ("PUT", "/api/v1/users/admin/password"),
    ("DELETE", "/api/v1/users/admin"),
    ("PUT", "/api/v1/groups/Viewers"),
    ("DELETE", "/api/v1/groups/Viewers"),
    ("POST", "/api/v1/tokens"),
    ("GET", "/api/v1/sessions?user=admin"),
    ("DELETE", "/api/v1/sessions/admin/0123456789abcdef"),
]
BODIES: dict[str, Any] = {
    "/api/v1/settings/location": {"latitude": 1, "longitude": 1, "timezone": "UTC"},
    "/api/v1/plugins/pump/pump": {"host": "192.0.2.20"},
    "/api/v1/secrets/pump.psk": {"value": "x"},
    "/api/v1/users": {"name": "x", "password": ADMIN_PASSWORD},
    "/api/v1/users/admin/groups": {"groups": []},
    "/api/v1/users/admin/password": {"password": ADMIN_PASSWORD + "!"},
    "/api/v1/groups/Viewers": {"permissions": []},
    "/api/v1/tokens": {"name": "t", "permissions": ["points.read"]},
}


async def test_a_viewer_can_only_look(site: Site) -> None:
    accounts = site.services.accounts
    await accounts.create_user("admin", ADMIN_PASSWORD, ["Administrators"], by="cli")
    viewer = await accounts.create_user("vic", ADMIN_PASSWORD + " too", ["Viewers"], by="cli")
    raw = await accounts.start_session(viewer)
    session = await accounts.session(raw)
    assert session is not None
    from thermaestro.web.security import csrf_token

    headers = {"x-csrf-token": csrf_token(KEY, session.hash)}
    async with site.client() as client:
        client.cookies.set("thermaestro_session", raw)
        assert (await client.get("/api/v1/status")).status_code == 200
        for method, path in VIEWER_REFUSED:
            body = BODIES.get(path.split("?")[0])
            answer = await client.request(method, path, json=body, headers=headers)
            assert answer.status_code == 403, (method, path, answer.text)
        for page in ("/setup/house", "/setup/prices", "/system/health", "/users", "/audit"):
            assert (await client.get(page)).status_code == 403, page


# --- step-up -----------------------------------------------------------------------------


async def test_changes_to_users_need_a_recent_password(site: Site) -> None:
    async with admin(site) as client:
        site.clock.now += 16 * 60
        fields = {"name": "bo", "password": ADMIN_PASSWORD + " two", "groups": "Household"}
        token = csrf_of((await client.get("/users")).text)
        api = await client.post(
            "/api/v1/users",
            json=fields | {"groups": ["Household"]},
            headers={"x-csrf-token": token},
        )
        assert api.status_code == 403
        assert api.json()["confirm"] is True
        # The page asks for the password, and carries the change it was asked for.
        asked = await form(client, "/users", "/users", **fields)
        assert asked.status_code == 200
        assert 'action="/users"' in asked.text
        assert '<input type="hidden" name="name" value="bo">' in asked.text
        assert 'name="step_up_password"' in asked.text
        carried = dict(
            re.findall(r'<input type="hidden" name="([^"]+)" value="([^"]*)">', asked.text)
        )
        assert carried["groups"] == "Household"
        wrong = await client.post("/users", data={**carried, "step_up_password": "wrong" * 4})
        assert wrong.status_code == 400
        assert "isn&#39;t right" in wrong.text
        assert await site.services.accounts.user("bo") is None
        again = dict(
            re.findall(r'<input type="hidden" name="([^"]+)" value="([^"]*)">', wrong.text)
        )
        done = await client.post("/users", data={**again, "step_up_password": ADMIN_PASSWORD})
        assert (done.status_code, done.headers["location"]) == (303, "/users")
        # A page without a form to carry still goes through /confirm and back.
        site.clock.now += 16 * 60
        confirmed = await form(
            client, "/confirm", "/confirm", password=ADMIN_PASSWORD, next="/users"
        )
        assert confirmed.headers["location"] == "/users"
    assert await site.services.accounts.user("bo") is not None


# --- every UI action is in the API -------------------------------------------------------


def _actions(app: FastAPI, *, api: bool) -> set[str]:
    found = set()
    for route in routes(app):
        if route.path.startswith("/api/") == api:
            name = getattr(route.endpoint, "__action__", None)
            if name:
                found.add(name)
    return found


async def test_every_ui_action_has_an_api_counterpart(site: Site) -> None:
    ui, api = _actions(site.app, api=False), _actions(site.app, api=True)
    assert ui, "no UI actions found"
    assert ui <= api, f"UI actions without an API operation: {sorted(ui - api)}"


async def test_every_ui_form_posts_to_a_named_action(site: Site) -> None:
    unsafe = [
        r
        for r in routes(site.app)
        if not r.path.startswith("/api/") and (r.methods or set()) - {"GET"}
    ]
    assert len(unsafe) > 10
    for route in unsafe:
        assert getattr(route.endpoint, "__action__", None), route.path


# --- the pages ---------------------------------------------------------------------------


class Strict(HTMLParser):
    """Finds what a strict CSP would block: inline scripts, inline styles, handlers."""

    def __init__(self) -> None:
        super().__init__()
        self.problems: list[str] = []
        self._in_script = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        names = {name for name, _ in attrs}
        if tag == "script":
            if "src" not in names:
                self.problems.append("inline <script>")
            self._in_script = True
        if tag == "style":
            self.problems.append("<style>")
        if "style" in names:
            self.problems.append(f"style= on <{tag}>")
        self.problems += [f"{n}= on <{tag}>" for n in names if n.startswith("on")]
        for name, value in attrs:
            if value and value.strip().lower().startswith("javascript:"):
                self.problems.append(f"javascript: URL in {name}")

    def handle_data(self, data: str) -> None:
        if self._in_script and data.strip():
            self.problems.append("script content")

    def handle_endtag(self, tag: str) -> None:
        if tag == "script":
            self._in_script = False


def _with_a_pump(site: Site) -> None:
    pump = FakePump()
    described = pump.describe(1)
    host = site.services.host
    assert host is not None
    host.instances["pump"] = Instance(
        "pump", Plugin(plugin="fake"), state=State.UP, described=described
    )
    for point in described.points:
        site.services.values.add("pump", pump.envelope(point.path))


PAGES = [
    "/",
    "/status/values",
    "/points/pump/hp1/outdoor.temp",
    "/prices",
    "/weather",
    "/setup/house",
    "/setup/pump",
    "/setup/external",
    "/setup/sensors",
    "/setup/prices",
    "/setup/weather",
    "/system/health",
    "/users",
    "/audit",
    "/account",
    "/confirm",
]


async def test_the_colors_are_chosen_per_browser(site: Site) -> None:
    """As the device by default; light or dark once chosen, set on the page itself so it
    never shows in the other colors first, before login too."""
    async with admin(site) as client:
        page = (await client.get("/")).text
        assert '<html lang="en" data-theme="auto">' in page
        assert 'href="/theme/light?next=/"' in page
        chosen = await client.get("/theme/dark", params={"next": "/prices"})
        assert (chosen.status_code, chosen.headers["location"]) == (303, "/prices")
        page = (await client.get("/prices")).text
        assert 'data-theme="dark"' in page
        assert 'href="/theme/auto?next=/prices"' in page
        away = await client.get("/theme/light", params={"next": "https://example.com/"})
        assert away.headers["location"] == "/"
    async with site.client() as anonymous:
        anonymous.cookies.set("theme", "light")
        login = (await anonymous.get("/login")).text
    assert 'data-theme="light"' in login
    assert 'class="theme-switch"' in login


async def test_old_pages_lead_to_their_new_places(site: Site) -> None:
    async with admin(site) as client:
        for old, new in (
            ("/settings", "/setup/house"),
            ("/sensors", "/setup/sensors"),
            ("/rooms", "/setup/house#rooms"),
            ("/diagnostics", "/system/health"),
            ("/system", "/system/health"),
        ):
            answer = await client.get(old)
            assert (answer.status_code, answer.headers["location"]) == (303, new), old
        assert (await client.get("/setup/nowhere")).status_code == 404
        house = (await client.get("/setup/house")).text
    # Each topic is in the side list, the current one marked.
    for topic in ("house", "pump", "external", "sensors", "prices", "weather"):
        assert f'href="/setup/{topic}"' in house
    assert 'href="/setup/house" aria-current="page"' in house


async def test_pages_render_with_nothing_inline(site: Site) -> None:
    _with_a_pump(site)
    async with admin(site) as client:
        for path in [*PAGES, "/account"]:
            for language in ("en", "sv", "de"):
                answer = await client.get(path, headers={"accept-language": language})
                assert answer.status_code == 200, (path, answer.text[:500])
                checker = Strict()
                checker.feed(answer.text)
                assert checker.problems == [], (path, checker.problems)
    async with site.client() as anonymous:
        for path in ("/login", "/setup"):
            checker = Strict()
            checker.feed((await anonymous.get(path)).text)
            assert checker.problems == [], path


async def test_status_shows_values_with_their_quality(site: Site) -> None:
    _with_a_pump(site)
    async with admin(site) as client:
        page = (await client.get("/status/values")).text
        status = (await client.get("/api/v1/status")).json()
    assert "4.5 \N{DEGREE SIGN}C" in page
    assert 'title="not_connected: sensor not connected"' in page  # the why, on the value
    assert re.search(r">\s*30\s*<span", page)  # a whole number, not 30.0 (x.fake.prio)
    assert "Outdoor temperature" in page  # names for people
    # The pump's values by the part they belong to.
    assert "<h2>Fake FP-1</h2>" in page
    assert re.search(r"<h3>Climate system 1</h3>.*Supply temperature", page, re.S)
    assert 'title="hp1/cs1/supply.temp"' in page  # the path, for whoever needs it
    assert "quality-grey" in page
    assert "quality-green" in page
    [pump] = status
    supply = next(p for p in pump["points"] if p["path"] == "hp1/cs1/supply.temp")
    assert (supply["value"], supply["quality"]) == (None, "not_connected")


async def test_numbers_follow_the_language(site: Site) -> None:
    _with_a_pump(site)
    async with admin(site) as client:
        swedish = await client.get("/status/values", headers={"accept-language": "sv-SE,sv;q=0.9"})
        chosen = await client.get("/lang/de?next=/status/values", follow_redirects=True)
    assert "4,5 \N{DEGREE SIGN}C" in swedish.text
    assert "4,5 \N{DEGREE SIGN}C" in chosen.text
    assert '<html lang="de"' in (await _page(site, "/login", lang="de"))


async def _page(site: Site, path: str, lang: str) -> str:
    async with site.client() as client:
        client.cookies.set("lang", lang)
        return (await client.get(path)).text


async def test_the_pump_connection_and_its_key(site: Site) -> None:
    key = "ab" * 32
    async with admin(site) as client:
        answer = await form(
            client,
            "/setup/pump",
            "/settings/pump",
            id="pump",
            host="192.0.2.20",
            protocol="thermaestro-gw",
            psk=key,
        )
        assert answer.status_code == 303, answer.text
        page = (await client.get("/setup/pump")).text
        missing = await form(
            client,
            "/setup/pump",
            "/settings/pump",
            id="pump2",
            host="192.0.2.21",
            protocol="nonsense",
        )
        assert missing.status_code == 400
    assert key not in page
    assert "entered; leave empty to keep it" in page
    assert "Secrets" not in page  # nothing to do with them there
    setting = await site.services.db.get(Plugin, "pump")
    assert setting is not None
    assert setting.settings["psk"] == "pump.psk"
    assert setting.settings["host"] == "192.0.2.20"
    stored = await site.services.secrets.get("pump.psk")
    assert stored is not None
    assert stored.get_secret_value() == key
    audit = (site.state / "audit" / "audit.jsonl").read_text()
    assert key not in audit
    assert '"what":"secret.set"' in audit


async def test_history_and_the_chart_page(site: Site) -> None:
    _with_a_pump(site)
    await site.services.values.flush()
    async with admin(site) as client:
        page = await client.get("/points/pump/hp1/outdoor.temp")
        source = re.search(r'data-source="([^"]+)"', page.text)
        assert source
        import time

        now = time.time()
        samples = await client.get(source.group(1), params={"start": now - 60, "end": now + 60})
        too_long = await client.get(source.group(1), params={"start": 0, "end": now})
    assert [s["value"] for s in samples.json()] == [4.5]
    assert too_long.status_code == 400


async def test_logset_download(site: Site) -> None:
    async with admin(site) as client:
        answer = await client.get("/api/v1/nibe/logset", params={"model": "F1245"})
        unknown = await client.get("/api/v1/nibe/logset", params={"model": "X9"})
    assert answer.status_code == 200
    assert answer.content.startswith(b"[NIBL;")
    assert "LOG.SET" in answer.headers["content-disposition"]
    assert unknown.status_code == 404


async def test_own_sessions_and_tokens(site: Site) -> None:
    async with admin(site) as client:
        sessions = (await client.get("/api/v1/sessions")).json()
        assert len(sessions) == 1
        async with admin(site) as other:
            ended = await form(other, "/account", f"/account/sessions/{sessions[0]['id']}/end")
            assert ended.headers["location"] == "/account"
        assert (await client.get("/")).status_code == 303  # that was this one


async def test_the_audit_page(site: Site) -> None:
    async with admin(site) as client:
        page = await client.get("/audit")
        entries = (await client.get("/api/v1/audit", params={"limit": 5})).json()
    assert "login" in page.text
    assert entries[0]["what"] == "login"
    assert json.dumps(entries)  # plain JSON


async def test_the_api_login_takes_json_from_scripts_only(site: Site) -> None:
    await site.services.accounts.create_user("admin", ADMIN_PASSWORD, ["Administrators"], by="cli")
    body = {"name": "admin", "password": ADMIN_PASSWORD}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=site.app), base_url=ORIGIN
    ) as script:
        answer = await script.post("/api/v1/login", json=body)
        assert answer.status_code == 200
        token = answer.json()["csrf"]
        status = await script.get("/api/v1/status")
        assert status.status_code == 200
        unsafe = await script.post(
            "/api/v1/logout", headers={"origin": ORIGIN, "x-csrf-token": token}
        )
        assert unsafe.status_code == 200
    async with site.client(origin="http://evil.example") as other_site:
        assert (await other_site.post("/api/v1/login", json=body)).status_code == 403
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=site.app), base_url=ORIGIN
    ) as script:
        # What a form on another site could send: refused, not taken as a login.
        as_form = await script.post("/api/v1/login", data=body)
        assert as_form.status_code in (415, 422)
        assert "thermaestro_session" not in as_form.cookies


async def test_a_reloaded_part_sends_the_whole_page_to_the_login(site: Site) -> None:
    async with site.client() as client:
        answer = await client.get("/status/values", headers={"hx-request": "true"})
    assert answer.status_code == 401
    assert answer.headers["hx-redirect"] == "/login"
    assert answer.text == ""


async def test_the_time_zone_is_picked_from_a_list(site: Site) -> None:
    async with admin(site) as client:
        page = (await client.get("/setup/house")).text
        assert '<option value="Europe/Stockholm">Stockholm</option>' in page
        assert '<optgroup label="Europe">' in page
        assert "data-detect-zone" in page  # none chosen yet: the browser's is preselected
        assert 'value="US/Eastern"' not in page  # old aliases left out
        assert 'value="Etc/GMT+1"' not in page
        saved = await form(
            client,
            "/setup/house",
            "/settings/location",
            latitude="52.52",
            longitude="13.40",
            timezone="Europe/Berlin",
        )
        assert saved.status_code == 303
        page = (await client.get("/setup/house")).text
        assert '<option value="Europe/Berlin" selected>Berlin</option>' in page
        assert "data-detect-zone" not in page
        refused = await form(
            client,
            "/setup/house",
            "/settings/location",
            latitude="52.52",
            longitude="13.40",
            timezone="Mars/Olympus_Mons",
        )
        assert refused.status_code == 400


async def test_each_user_keeps_their_language_and_formats(site: Site) -> None:
    _with_a_pump(site)
    async with admin(site) as client:
        await form(
            client,
            "/setup/house",
            "/settings/location",
            latitude="59.33",
            longitude="18.07",
            timezone="Europe/Stockholm",
        )
        saved = await form(client, "/account", "/account/preferences", language="en", region="SE")
        assert saved.status_code == 303
        # The browser asks for German; the user's choice wins.
        page = await client.get("/pump", headers={"accept-language": "de-DE"})
        assert "4,5 \N{DEGREE SIGN}C" in page.text
        assert "Observed" in page.text  # English
        assert re.search(r"\d{4}-\d{2}-\d{2}, \d{2}:\d{2}", page.text)  # ISO, 24 h
        account = (await client.get("/account")).text
        assert '<option value="SE" selected>Sweden</option>' in account
        api = await client.get("/api/v1/account/preferences")
        assert api.json() == {"language": "en", "region": "SE"}
        token = csrf_of(account)
        changed = await client.put(
            "/api/v1/account/preferences",
            json={"language": "sv", "decimal": "point"},
            headers={"x-csrf-token": token},
        )
        assert changed.json() == {"language": "sv", "decimal": "point"}
        page = await client.get("/pump")
        assert "4.5 \N{DEGREE SIGN}C" in page.text
        assert "Uppmätt" in page.text
        refused = await client.put(
            "/api/v1/account/preferences",
            json={"language": "fr"},
            headers={"x-csrf-token": token},
        )
        assert refused.status_code == 400
        chart = (await client.get("/points/pump/hp1/outdoor.temp")).text
        assert 'data-zone="Europe/Stockholm"' in chart
        assert 'data-locale="sv"' in chart


async def test_languages_before_login_and_after(site: Site) -> None:
    async with site.client() as client:
        login = (await client.get("/login")).text
    header = login.split("</header>")[0]
    assert 'href="/lang/sv?next=/login"' in header  # at the top, where it's seen
    async with admin(site) as client:
        status = (await client.get("/")).text
        account = (await client.get("/account")).text
    assert "/lang/" not in status
    assert "<footer" not in status
    assert 'id="formats"' in account


async def test_the_users_page_explains_the_rights(site: Site) -> None:
    async with admin(site) as client:
        page = (await client.get("/users")).text
    assert '<dl class="rights">' in page
    assert "<code>audit.read</code></dt><dd>read the audit log</dd>" in page


async def test_implausible_values_show_red(site: Site) -> None:
    _with_a_pump(site)
    envelope = FakePump().envelope("hp1/outdoor.temp").model_copy(update={"value": 900.0})
    site.services.values.add("pump", envelope)
    async with admin(site) as client:
        page = (await client.get("/status/values")).text
    assert "quality-red" in page
    assert "implausible: outside -60..150 degC" in page


async def test_a_point_page_shows_its_description(site: Site) -> None:
    _with_a_pump(site)
    host = site.services.host
    assert host is not None
    instance = host.instances["pump"]
    assert instance.described is not None
    points = tuple(
        p.model_copy(update={"description": "Current outdoor temperature"})
        if p.path == "hp1/outdoor.temp"
        else p
        for p in instance.described.points
    )
    instance.described = instance.described.model_copy(update={"points": points})
    async with admin(site) as client:
        page = (await client.get("/points/pump/hp1/outdoor.temp")).text
    assert '<p class="description">Current outdoor temperature</p>' in page


async def test_the_overview_lists_devices_and_what_needs_attention(site: Site) -> None:
    """A plugin with no values isn't listed; one that is down, or asks for something, is
    under "Needs attention", with a link to where it's set up."""
    _with_a_pump(site)
    host = site.services.host
    assert host is not None
    setting = Plugin(plugin="tibber", settings={"token": "tibber.token"})
    host.instances["tibber"] = Instance("tibber", setting, state=State.UP)
    async with admin(site) as client:
        calm = (await client.get("/status/values")).text
        host.instances["tibber"].state = State.RESTARTING
        host.instances["tibber"].last_error = "the token was refused"
        troubled = (await client.get("/status/values")).text
    assert "tibber" not in calm
    assert "Needs attention" not in calm
    assert "Needs attention" in troubled
    assert "the token was refused" in troubled
    assert 'href="/setup/prices"' in troubled


async def test_the_price_and_weather_at_a_glance(site: Site) -> None:
    """The overview's small charts sit between its two reloading parts, drawn once; only
    for those who may see the prices and the weather."""
    _with_a_pump(site)
    async with admin(site) as client:
        page = (await client.get("/")).text
        top = (await client.get("/status/values", params={"part": "top"})).text
        devices = (await client.get("/status/values", params={"part": "devices"})).text
    assert page.index('id="values-top"') < page.index("data-chart=") < page.index("data-meteogram=")
    assert page.index("data-meteogram=") < page.index('id="values-devices"')
    assert 'data-compact="1" data-now="price-now"' in page
    assert "Fake FP-1" not in top
    assert "Fake FP-1" in devices
    viewer = await site.services.accounts.create_user(
        "vic", ADMIN_PASSWORD + " too", ["Viewers"], by="cli"
    )
    raw = await site.services.accounts.start_session(viewer)
    async with site.client() as client:
        client.cookies.set("thermaestro_session", raw)
        page = (await client.get("/")).text
    assert "data-chart=" not in page
    assert "Fake FP-1" in page


async def test_named_values_are_charted_as_bands(site: Site) -> None:
    """A demand or a switch has no line to draw: its chart is bands over time, with the
    value names in the page's language."""
    _with_a_pump(site)
    pump = FakePump()
    site.services.values.add(
        "pump", pump.envelope("hp1/x.fake.prio").model_copy(update={"value": "dhw"})
    )
    site.services.values.add(
        "pump", pump.envelope("hp1/cs1/x.fake.offset").model_copy(update={"value": True})
    )
    async with admin(site) as client:
        number = (await client.get("/points/pump/hp1/outdoor.temp")).text
        named = (await client.get("/points/pump/hp1/x.fake.prio")).text
        switch = (await client.get("/points/pump/hp1/cs1/x.fake.offset")).text
        await client.put(
            "/api/v1/account/preferences",
            json={"language": "sv"},
            headers={"x-csrf-token": csrf_of((await client.get("/account")).text)},
        )
        swedish = (await client.get("/points/pump/hp1/x.fake.prio")).text
    assert 'data-kind="number"' in number
    assert 'data-kind="state"' in named
    assert 'data-kind="switch"' in switch
    assert '"dhw": "Hot water"' in named
    assert '"dhw": "Varmvatten"' in swedish


def test_values_are_shown_in_sentence_case() -> None:
    from thermaestro.web import labels

    assert labels.value("idle") == "Idle"  # a standard value
    assert labels.value("Auto") == "Auto"  # the pump's own
    assert labels.value("on") == "On"
    assert labels.value("hot water") == "Hot water"


async def test_settings_and_diagnostics_are_off_the_status_page(site: Site) -> None:
    _with_a_pump(site)
    host = site.services.host
    assert host is not None
    instance = host.instances["pump"]
    assert instance.described is not None
    points = tuple(
        p.model_copy(update={"category": "config"}) if p.path == "hp1/x.fake.prio" else p
        for p in instance.described.points
    )
    instance.described = instance.described.model_copy(update={"points": points})
    async with admin(site) as client:
        status = (await client.get("/status/values")).text
        everyday = (await client.get("/pump")).text
        settings = (await client.get("/pump", params={"show": "config"})).text
    assert "hp1/x.fake.prio" not in status
    assert "Outdoor temperature" in status
    assert 'href="/pump"' in status
    assert "hp1/x.fake.prio" not in everyday
    assert "hp1/x.fake.prio" in settings
    assert "Outdoor temperature" not in settings
