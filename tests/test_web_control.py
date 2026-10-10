"""Control over the API: asking for intents and ending them, levels, setup's answers about
the house, the levers' modes, and the plan."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest
from test_core_executor import OFFSET, Rig, rig
from test_web import ADMIN_PASSWORD, FAST, KEY, ORIGIN, csrf_of, form

from thermaestro.auth import Accounts, AddressLimiter, SetupCode
from thermaestro.core import AuditLog, Values
from thermaestro.intents import Capabilities, Intents
from thermaestro.store import Control, Home, SecretStore
from thermaestro.web import Services, create_app

CS = "dev:hp1"
MODE = "dev:hp1/mode"


@asynccontextmanager
async def site(tmp_path: Path) -> AsyncIterator[tuple[Rig, Services, httpx.AsyncClient]]:
    async with rig(tmp_path) as r:
        audit = AuditLog(tmp_path / "web-audit")
        accounts = Accounts(r.db, audit, hasher=FAST, limiter=AddressLimiter(tries=1000))
        caps = Capabilities(systems=frozenset({CS}), tanks=frozenset({CS}))
        services = Services(
            accounts=accounts,
            db=r.db,
            values=Values(r.db),
            host=r.host,
            audit=audit,
            secrets=SecretStore(tmp_path / "secrets.json"),
            setup=SetupCode(tmp_path / "setup-code"),
            intents=Intents(r.db, audit, capabilities=lambda: caps),
            executor=r.executor,
        )
        await accounts.create_user("admin", ADMIN_PASSWORD, ["Administrators"], by="cli")
        await accounts.create_user("kid", ADMIN_PASSWORD, ["Household"], by="cli")
        app = create_app(services, KEY)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=ORIGIN, headers={"origin": ORIGIN}
        ) as client:
            yield r, services, client


async def login(client: httpx.AsyncClient, name: str) -> dict[str, str]:
    """Log in; the headers an API call from that session needs."""
    page = await client.get("/login")
    answer = await client.post(
        "/login", data={"csrf": csrf_of(page.text), "name": name, "password": ADMIN_PASSWORD}
    )
    assert answer.status_code == 303, answer.text
    return {"x-csrf-token": csrf_of((await client.get("/account")).text)}


async def test_asking_for_an_intent_and_ending_it(tmp_path: Path) -> None:
    async with site(tmp_path) as (_, _, client):
        headers = await login(client, "admin")
        asked = await client.post(
            "/api/v1/intents",
            json={"kind": "warmer", "scope": CS, "offset": 1, "until": "2099-01-01T00:00:00Z"},
            headers=headers,
        )
        assert asked.status_code == 201, asked.text
        answer = asked.json()
        assert answer["accepted"]
        assert answer["intent"]["principal"] == "user:admin"
        assert answer["messages"][0].startswith("Warmer, please (+1 °C): until 2099-01-01")
        listed = (await client.get("/api/v1/intents")).json()
        assert [i["id"] for i in listed] == [answer["intent"]["id"]]
        ended = await client.delete(f"/api/v1/intents/{answer['intent']['id']}", headers=headers)
        assert ended.json()["state"] == "finished"
        assert (await client.get("/api/v1/intents")).json() == []


async def test_a_request_takes_only_its_own_fields(tmp_path: Path) -> None:
    async with site(tmp_path) as (_, _, client):
        headers = await login(client, "admin")
        wrong = await client.post(
            "/api/v1/intents",
            json={"kind": "warmer", "scope": CS, "offset": 1, "at_least": 50},
            headers=headers,
        )
        assert wrong.status_code == 400
        assert wrong.json()["error"] == "warmer doesn't take at_least"
        missing = await client.post("/api/v1/intents", json={"kind": "bath"}, headers=headers)
        assert missing.json()["error"] == "bath needs at_least, by"


async def test_the_household_asks_for_a_while_but_doesnt_set_the_stance(tmp_path: Path) -> None:
    async with site(tmp_path) as (_, _, client):
        headers = await login(client, "kid")
        warmer = await client.post(
            "/api/v1/intents", json={"kind": "warmer", "scope": CS, "offset": -1}, headers=headers
        )
        assert warmer.status_code == 201
        stance = await client.post(
            "/api/v1/intents", json={"kind": "cost_stance", "slider": 1}, headers=headers
        )
        assert stance.status_code == 403
        assert "intent." in stance.json()["error"]
        levers = await client.put(
            f"/api/v1/levers/{OFFSET}/mode", json={"mode": "control"}, headers=headers
        )
        assert levers.status_code == 403


async def test_levels(tmp_path: Path) -> None:
    async with site(tmp_path) as (_, _, client):
        headers = await login(client, "admin")
        put = await client.put(
            "/api/v1/levels/day",
            json={"name": "Day", "scope": CS, "low": 20.5, "high": 22},
            headers=headers,
        )
        assert put.json()["id"] == "day"
        assert [lv["id"] for lv in (await client.get("/api/v1/levels")).json()] == ["day"]
        band = {
            "kind": "comfort_band",
            "scope": CS,
            "pattern": [{"level": "day", "days": [0, 1, 2, 3, 4]}],
        }
        assert (await client.post("/api/v1/intents", json=band, headers=headers)).json()["accepted"]
        in_use = await client.delete("/api/v1/levels/day", headers=headers)
        assert in_use.status_code == 400
        assert "is used by" in in_use.json()["error"]
        bounds = (await client.get("/api/v1/intents/in-force")).json()["bounds"]
        assert all(b["target"] == "room_temp" for b in bounds)


async def test_setup_answers_about_the_house(tmp_path: Path) -> None:
    async with site(tmp_path) as (_, _, client):
        headers = await login(client, "admin")
        body = {"emitters": {CS: "floor"}, "house": "average", "water": "well", "holidays": "SE"}
        put = await client.put("/api/v1/home", json=body, headers=headers)
        assert put.status_code == 200, put.text
        assert (await client.get("/api/v1/home")).json()["emitters"] == {CS: "floor"}
        bad = await client.put("/api/v1/home", json={"water": "lake"}, headers=headers)
        assert bad.status_code == 400


async def test_a_lever_off_in_shadow_and_in_control(tmp_path: Path) -> None:
    async with site(tmp_path) as (r, _, client):
        headers = await login(client, "admin")
        levers = {lv["lever"]: lv for lv in (await client.get("/api/v1/levers")).json()}
        assert levers[OFFSET]["mode"] == "off"
        assert levers[MODE]["competing"] == [{"name": "the schedule", "confirmed_off": False}]
        shadow = await client.put(
            f"/api/v1/levers/{OFFSET}/mode", json={"mode": "shadow"}, headers=headers
        )
        assert shadow.status_code == 200, shadow.text
        assert (await r.db.get(Control) or Control()).levers == {OFFSET: "shadow"}
        unknown = await client.put(
            f"/api/v1/levers/{MODE}/confirmed-off", json={"features": ["nope"]}, headers=headers
        )
        assert unknown.status_code == 400
        confirmed = await client.put(
            f"/api/v1/levers/{MODE}/confirmed-off",
            json={"features": ["the schedule"]},
            headers=headers,
        )
        assert confirmed.status_code == 200
        levers = {lv["lever"]: lv for lv in (await client.get("/api/v1/levers")).json()}
        assert levers[MODE]["competing"] == [{"name": "the schedule", "confirmed_off": True}]
        bad = await client.put(
            f"/api/v1/levers/{OFFSET}/mode", json={"mode": "on"}, headers=headers
        )
        assert bad.status_code == 400
        missing = await client.put(
            "/api/v1/levers/dev:hp1/nothing/mode", json={"mode": "shadow"}, headers=headers
        )
        assert missing.status_code == 404


@pytest.mark.parametrize("path", ["/api/v1/plan", "/api/v1/intents", "/api/v1/levels"])
async def test_a_viewer_sees_nothing_of_control(tmp_path: Path, path: str) -> None:
    async with site(tmp_path) as (_, services, client):
        await services.accounts.create_user("guest", ADMIN_PASSWORD, ["Viewers"], by="cli")
        await login(client, "guest")
        assert (await client.get(path)).status_code == 403


async def test_the_plan_before_the_planner_runs(tmp_path: Path) -> None:
    async with site(tmp_path) as (_, _, client):
        await login(client, "admin")
        assert (await client.get("/api/v1/plan")).json() == {
            "at": None,
            "decisions": [],
            "notices": [],
        }


# --- the pages ----------------------------------------------------------------------------


async def test_the_control_page_switches_a_lever(tmp_path: Path) -> None:
    async with site(tmp_path) as (r, _, client):
        await login(client, "admin")
        page = (await client.get("/setup/control")).text
        assert "the schedule is off" in page
        assert f'action="/levers/{OFFSET}/mode"' in page
        assert "alarm" not in page.lower()  # a person's lever, not a setting to hand over
        done = await form(client, "/setup/control", f"/levers/{OFFSET}/mode", mode="shadow")
        assert done.status_code == 303
        assert (await r.db.get(Control) or Control()).levers == {OFFSET: "shadow"}
        confirmed = await form(
            client, "/setup/control", f"/levers/{MODE}/confirmed-off", features="the schedule"
        )
        assert confirmed.status_code == 303
        assert (await r.db.get(Control) or Control()).confirmed_off == {MODE: ("the schedule",)}
        refused = await form(client, "/setup/control", f"/levers/{OFFSET}/mode", mode="on")
        assert refused.status_code == 400
        assert "a lever is off, shadow, control" in refused.text


async def test_the_household_sees_the_settings_but_cant_switch_them(tmp_path: Path) -> None:
    async with site(tmp_path) as (_, _, client):
        await login(client, "kid")
        page = await client.get("/setup/control")
        assert page.status_code in (200, 403)
        if page.status_code == 200:
            assert f'action="/levers/{OFFSET}/mode"' not in page.text


async def test_the_house_questions(tmp_path: Path) -> None:
    async with site(tmp_path) as (r, _, client):
        await login(client, "admin")
        page = (await client.get("/setup/house")).text
        assert 'name="past_deadline"' in page
        done = await form(
            client,
            "/setup/house",
            "/settings/home",
            house="well_insulated",
            water="well",
            holidays="se",
            holidays_as="6",
            past_deadline="stop",
        )
        assert done.status_code == 303, done.text
        home = await r.db.get(Home)
        assert home is not None
        assert (home.house, home.water, home.holidays, home.holidays_as) == (
            "well_insulated",
            "well",
            "SE",
            6,
        )
        assert home.past_deadline == "stop"


# --- the Intents page and the overview -----------------------------------------------------


async def test_asking_from_the_intents_page(tmp_path: Path) -> None:
    async with site(tmp_path) as (_, services, client):
        await login(client, "admin")
        assert services.intents is not None
        assert (await client.get("/intents")).status_code == 200
        level = await form(
            client, "/intents", "/levels/new", name="Day time", scope=CS, low="20,5", high="22"
        )
        assert level.status_code == 303
        assert set(await services.intents.levels()) == {"day-time"}
        band = await form(
            client,
            "/intents",
            "/intents",
            kind="comfort_band",
            scope=CS,
            **{"p0.level": "day-time", "p0.days": "0", "p0.start": "06:00", "p0.end": "22:00"},
        )
        assert band.status_code == 200, band.text
        assert "Asked for." in band.text
        warmer = await form(client, "/intents", "/intents", kind="warmer", scope=CS, offset="1")
        assert warmer.status_code == 200
        assert "Warmer, please (+1 °C): until the next change" in warmer.text
        kinds = {i.kind: i for i in await services.intents.all()}
        assert set(kinds) == {"comfort_band", "warmer"}
        assert kinds["comfort_band"].expectations[0].contexts[0].days == (0,)
        ended = await form(client, "/intents", f"/intents/{kinds['warmer'].id}/end", next="/")
        assert ended.status_code == 303
        assert ended.headers["location"] == "/"
        bad = await form(client, "/intents", "/intents", kind="warmer", scope=CS, offset="x")
        assert bad.status_code == 400
        assert "offset is a number" in bad.text


async def test_the_overview_asks_for_a_while(tmp_path: Path) -> None:
    async with site(tmp_path) as (_, _, client):
        await login(client, "kid")
        page = await client.get("/")
        assert page.status_code == 200
        assert "Ask for a while" in page.text
        assert 'value="hands_off"' not in page.text  # the household has no right to it
