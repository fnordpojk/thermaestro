"""Control over the API: asking for intents and ending them, levels, setup's answers about
the house, the levers' modes, and the plan."""

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from test_core_executor import OFFSET, Rig, rig
from test_web import ADMIN_PASSWORD, FAST, KEY, ORIGIN, csrf_of, form

from thermaestro.auth import Accounts, AddressLimiter, SetupCode
from thermaestro.core import AuditLog, Values
from thermaestro.intents import Capabilities, Intents, Level, kinds
from thermaestro.intents.resolve import RANKS
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
        body = {"emitters": {CS: "slab"}, "house": "average", "water": "well", "holidays": "SE"}
        put = await client.put("/api/v1/home", json=body, headers=headers)
        assert put.status_code == 200, put.text
        assert (await client.get("/api/v1/home")).json()["emitters"] == {CS: "slab"}
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


async def test_a_baseline_and_a_persons_write(tmp_path: Path) -> None:
    async with site(tmp_path) as (r, _, client):
        headers = await login(client, "admin")
        put = await client.put(
            f"/api/v1/levers/{OFFSET}/baseline", json={"value": 2}, headers=headers
        )
        assert put.status_code == 200, put.text
        levers = {lv["lever"]: lv for lv in (await client.get("/api/v1/levers")).json()}
        assert levers[OFFSET]["baseline"] == 2
        assert levers[MODE]["choices"] == ["eco", "normal"]
        bad = await client.put(
            f"/api/v1/levers/{OFFSET}/baseline", json={"value": 99}, headers=headers
        )
        assert (bad.status_code, bad.json()["error"]) == (400, "99 is above 10")
        wrote = await client.post(
            "/api/v1/devices/dev/write",
            json={"point": "hp1/x.fake.start", "value": 50},
            headers=headers,
        )
        assert wrote.json() == {"outcome": "verified", "detail": None}
        assert f'action="/levers/{OFFSET}/baseline"' in (await client.get("/setup/control")).text
        done = await form(client, "/setup/control", f"/levers/{OFFSET}/baseline", value="-1")
        assert done.status_code == 303
        assert r.executor.claims[OFFSET].baseline == -1
        await form(client, "/", "/logout")
        household = await login(client, "kid")
        refused = await client.post(
            "/api/v1/devices/dev/write",
            json={"point": "hp1/x.fake.start", "value": 40},
            headers=household,
        )
        assert refused.status_code == 403
        assert r.device.registers["x.fake.start"] == 50


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
            "ahead": [],
            "house_kw": None,
            "limit_kw": None,
            "limit_why": None,
            "ranking": [],
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
        cooler = await form(client, "/intents", "/intents", kind="warmer", scope=CS, offset="-1")
        assert "Cooler, please (-1 °C): until the next change" in cooler.text
        listed = (await client.get("/intents")).text
        assert "<summary>Cooler, please (-1 °C)" in listed
        assert "Warmer, please (-1 °C)" not in listed
        kinds = {i.kind: i for i in await services.intents.all()}
        assert set(kinds) == {"comfort_band", "warmer"}
        assert kinds["comfort_band"].expectations[0].contexts[0].days == (0,)
        ended = await form(client, "/intents", f"/intents/{kinds['warmer'].id}/end", next="/")
        assert ended.status_code == 303
        assert ended.headers["location"] == "/"
        bad = await form(client, "/intents", "/intents", kind="warmer", scope=CS, offset="x")
        assert bad.status_code == 400
        assert "offset is a number" in bad.text


async def test_what_was_asked_for_folds_out(tmp_path: Path) -> None:
    async with site(tmp_path) as (_, _, client):
        await login(client, "admin")
        await form(
            client, "/intents", "/levels/new", name="Day time", scope=CS, low="20,5", high="22"
        )
        await form(
            client,
            "/intents",
            "/intents",
            kind="comfort_band",
            scope=CS,
            **{"p0.level": "day-time", "p0.days": "0", "p0.start": "06:00", "p0.end": "22:00"},
        )
        ranks = ["comfort_low", "must_deadlines", "should_deadlines", "comfort_high", "power_peak"]
        await form(
            client,
            "/intents",
            "/intents",
            kind="cost_stance",
            slider="40",
            **{f"rank{n}": key for n, key in enumerate(ranks, 1)},
        )
        page = (await client.get("/intents")).text
        assert page.count('class="intent"') == 2
        band = "<li>Day time: rooms 20.5\N{EN DASH}22.0 °C: Monday, 6:00"  # the clock's own
        assert band in page
        assert "<li>Should be met; may give way.</li>" in page
        assert "<li>Savings: 40 %</li>" in page
        assert "<li>Rooms not below their band</li>" in page


async def test_changing_what_was_asked_for(tmp_path: Path) -> None:
    async with site(tmp_path) as (_, services, client):
        headers = await login(client, "admin")
        assert services.intents is not None
        await form(
            client, "/intents", "/levels/new", name="Day time", scope=CS, low="20,5", high="22"
        )
        day = {"p0.level": "day-time", "p0.start": "06:00", "p0.end": "22:00"}
        await form(client, "/intents", "/intents", kind="comfort_band", scope=CS, **day)
        await form(client, "/intents", "/intents", kind="warmer", scope=CS, offset="1")
        found = {i.kind: i for i in await services.intents.all()}
        band, warmer = found["comfort_band"], found["warmer"]
        page = (await client.get("/intents")).text
        assert f'action="/intents/{band.id}/edit"' in page
        assert 'name="p0.start" value="06:00"' in page  # filled in with what it holds
        # A step added in the spare row.
        saturday = {"p1.level": "day-time", "p1.days": "5", "p1.start": "08:00", "p1.end": "23:00"}
        changed = await form(
            client, "/intents", f"/intents/{band.id}/edit", scope=CS, **day, **saturday
        )
        assert changed.status_code == 200, changed.text
        assert "Asked for." in changed.text
        kept = await services.intents.get(band.id)
        assert (kept.id, len(kept.expectations)) == (band.id, 2)
        # Over the API: the whole request, its kind left out.
        put = await client.put(
            f"/api/v1/intents/{warmer.id}", json={"scope": CS, "offset": -1}, headers=headers
        )
        assert put.status_code == 200, put.text
        assert put.json()["accepted"]
        assert (await services.intents.get(warmer.id)).parameters == {"offset": -1.0}
        # A level changed in place: every intent naming it follows.
        assert 'action="/levels/day-time"' in page
        moved = await form(
            client, "/intents", "/levels/day-time", name="Day", scope=CS, low="21", high="22,5"
        )
        assert moved.status_code == 303
        level = (await services.intents.levels())["day-time"]
        assert (level.name, level.low, level.high) == ("Day", 21, 22.5)


async def test_every_kind_can_be_changed_on_the_page(tmp_path: Path) -> None:
    async with site(tmp_path) as (_, services, client):
        await login(client, "admin")
        intents = services.intents
        assert intents is not None
        admin = frozenset({"*"})
        for level in (
            Level(id="day", name="Day", scope=CS, low=20.5, high=22.0),
            Level(id="more", name="More", scope=CS, top=55.0),
        ):
            await intents.put_level(level, who="admin", granted=admin)
        now = datetime.now(UTC)
        who: dict[str, Any] = {"principal": "user:admin", "created": now}
        soon = now + timedelta(hours=20)
        asked = [
            kinds.comfort_band(CS, [("day", ((0,), time(6), time(22)))], **who),
            kinds.hot_water_by(CS, [("more", (5,), time(7)), (52.0, (), time(19))], **who),
            kinds.hot_water_floor(CS, 40.0, **who),
            kinds.cost_stance(ranking=RANKS, slider=0.4, **who),
            kinds.addition_policy("limit", kw=3.0, **who),
            kinds.quiet_hours([((), time(22), time(7))], **who),
            kinds.warmer(CS, 1.0, until=soon, **who),
            kinds.bath(CS, 52, soon, **who),
            kinds.guests(soon, {CS: "day"}, hot_water="more", **who),
            kinds.hands_off(soon, **who),
            kinds.fireplace(CS, **who),
            kinds.boost_now(CS, **who),
        ]
        for intent in asked:
            assert (await intents.create(intent, granted=admin)).accepted, intent.kind
        page = await client.get("/intents")
        assert page.status_code == 200
        kept = await intents.all()
        assert {i.kind for i in kept} >= {"hot_water_by", "cost_stance", "guests", "bath"}
        for intent in kept:
            assert f'action="/intents/{intent.id}/edit"' in page.text, intent.kind


async def test_shadow_beside_the_pump(tmp_path: Path) -> None:
    async with site(tmp_path) as (r, _, client):
        headers = await login(client, "admin")
        await r.executor.set_mode(OFFSET, "shadow", who="admin")
        assert (await r.executor.act(OFFSET, "set", {"value": 2}, who="planner")).outcome == (
            "shadowed"
        )
        page = await client.get("/shadow")
        assert page.status_code == 200
        assert "set to 2" in page.text
        assert "shadow-chart" in page.text
        logged = (await client.get("/api/v1/shadow?days=1", headers=headers)).json()
        assert [(x["lever"], x["params"]) for x in logged] == [(OFFSET, {"value": 2})]
        assert logged[0]["found"]["hp1/x.fake.offset"]["value"] == -4
        exported = await client.get(f"/api/v1/shadow.csv?lever={OFFSET}", headers=headers)
        assert exported.headers["content-type"].startswith("text/csv")
        lines = exported.text.splitlines()
        assert lines[0] == "time,lever,asked,value,the device showed,why"
        assert f"{OFFSET},set,2,hp1/x.fake.offset=-4" in lines[1]
        series = (
            await client.get(f"/api/v1/shadow/series?lever={OFFSET}&days=1", headers=headers)
        ).json()
        assert series["point"] == "hp1/x.fake.offset"
        assert [s["value"] for s in series["shadow"]] == [2]
        assert (await client.get("/shadow?day=nonsense")).status_code == 200  # said, not raised


async def test_now_says_how_far_price_may_move_the_target(tmp_path: Path) -> None:
    async with site(tmp_path) as (_, services, client):
        await login(client, "admin")
        assert services.intents is not None
        await form(
            client, "/intents", "/levels/new", name="Narrow", scope=CS, low="19", high="19,5"
        )
        await form(
            client, "/intents", "/intents", kind="comfort_band", scope=CS, **{"p0.level": "narrow"}
        )
        await form(client, "/intents", "/intents", kind="cost_stance", slider="100")
        page = (await client.get("/intents")).text
        assert "the band is too narrow for price to move the target" in page
        await form(
            client, "/intents", "/levels/narrow", name="Narrow", scope=CS, low="19", high="21"
        )
        page = (await client.get("/intents")).text
        assert "price may move the target up to 0.75 °C either way" in page  # 1 - 0.25


async def test_coming_from_nibepi(tmp_path: Path) -> None:
    from nibepi_configs import BROKER_PASSWORD, line_ours

    from thermaestro.store import Mqtt, Room, Sensor

    async with site(tmp_path) as (_, services, client):
        headers = await login(client, "admin")
        page = (await client.get("/setup/nibepi")).text
        assert 'enctype="multipart/form-data"' in page
        upload = {"config": ("config.json", json.dumps(line_ours()), "application/json")}
        read = await client.post("/setup/nibepi", data={"csrf": csrf_of(page)}, files=upload)
        assert read.status_code == 303, read.text
        location = read.headers["location"]
        draft = (await client.get(location)).text
        assert "the MQTT broker at localhost:1883" in draft
        assert BROKER_PASSWORD not in draft
        token = location.split("draft=")[1].split("#")[0]
        chosen = ["location:", "mqtt:", "room:living-room", "sensor:living-room", "sensor:hall"]
        made = await client.post(
            f"/setup/nibepi/{token}/confirm",
            data={"csrf": csrf_of(draft), "items": chosen, "timezone": "Europe/Stockholm"},
        )
        assert made.status_code == 200, made.text
        assert "Made:" in made.text
        mqtt = await services.db.get(Mqtt)
        assert mqtt is not None
        assert (mqtt.host, mqtt.password) == ("localhost", "mqtt.password")
        secret = await services.secrets.get("mqtt.password")
        assert secret is not None
        assert secret.get_secret_value() == BROKER_PASSWORD
        rooms = await services.db.all(Room)
        assert [r.name for r in rooms.values()] == ["Living room"]
        sensors = {s.name: s for s in (await services.db.all(Sensor)).values()}
        assert sensors["Living room"].room == next(iter(rooms))
        assert sensors["Hall"].room is None
        assert "Office" not in sensors  # not ticked
        gone = await client.post(
            f"/setup/nibepi/{token}/confirm", data={"csrf": csrf_of(draft), "items": chosen}
        )
        assert "that draft is gone" in gone.text
        # Over the API: the draft, with secrets by name only.
        answer = await client.post(
            "/api/v1/import/nibepi", json={"config": json.dumps(line_ours())}, headers=headers
        )
        assert answer.status_code == 200, answer.text
        body = answer.json()
        assert body["secrets"] == ["mqtt.password"]
        assert BROKER_PASSWORD not in answer.text
        assert {"key": "pump:pump", "optional": False}.items() <= next(
            i for i in body["items"] if i["kind"] == "pump"
        ).items()
        refused = await client.post("/api/v1/import/nibepi", json={"config": "{}"}, headers=headers)
        assert refused.status_code == 400


async def test_the_overview_asks_for_a_while(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with site(tmp_path) as (_, services, client):
        await login(client, "kid")
        page = await client.get("/")
        assert page.status_code == 200
        # Nothing to control yet (the pump not read, or none): nothing to ask for.
        assert "Ask for a while" not in page.text
        assert 'value="fireplace"' not in page.text
        scopes = {"systems": [(CS, "Radiators")], "tanks": [], "pools": [], "rooms": []}
        monkeypatch.setattr(services, "intent_scopes", lambda caller: scopes)
        page = await client.get("/")
        assert "Ask for a while" in page.text
        assert 'value="fireplace"' in page.text
        assert "Warmer" in page.text
        assert 'value="boost_now"' not in page.text  # no tank
        assert 'value="hands_off"' not in page.text  # the household has no right to it


async def test_saying_what_you_want(tmp_path: Path) -> None:
    """What is typed is answered in the conversation with a form to check; asking from it
    answers there too, and has the page show what applies at once."""
    chat = {"hx-request": "true"}
    async with site(tmp_path) as (_, services, client):
        headers = await login(client, "kid")
        token = csrf_of((await client.get("/")).text)
        said = await client.post("/ask", data={"csrf": token, "text": "bad kl 19.30"}, headers=chat)
        assert said.status_code == 200
        assert '<p class="said">bad kl 19.30</p>' in said.text
        assert 'name="kind" value="bath"' in said.text
        assert 'T19:30"' in said.text
        assert "<html" not in said.text  # lines of the conversation, not a page
        quiet = await client.post(
            "/ask", data={"csrf": token, "text": "away", "quiet": "1"}, headers=chat
        )
        assert 'class="said"' not in quiet.text  # a button's, not something said
        assert 'name="kind" value="away"' in quiet.text
        puzzled = await client.post(
            "/ask", data={"csrf": token, "text": "vad blir det för väder"}, headers=chat
        )
        assert "I didn't understand that" in puzzled.text
        read = await client.post(
            "/api/v1/intents/understand", json={"text": "extra varmvatten"}, headers=headers
        )
        assert (read.json()["kind"], read.json()["missing"]) == ("boost_now", [])
        asked = await client.post(
            "/intents", data={"csrf": token, "kind": "fireplace"}, headers=chat
        )
        assert "Asked for." in asked.text, asked.text
        assert asked.headers["hx-trigger"] == "page-changed"
        assert "<html" not in asked.text
        assert services.intents is not None
        assert [i.kind for i in await services.intents.all()] == ["fireplace"]
        refused = await client.post(
            "/intents", data={"csrf": token, "kind": "warmer", "offset": "x"}, headers=chat
        )
        assert "offset is a number" in refused.text
        assert "hx-trigger" not in refused.headers
        # Without scripts, the answer is a page of its own.
        page = await client.post("/ask", data={"csrf": token, "text": "a bath at 19:30"})
        assert "<h1>Ask for a while</h1>" in page.text


async def test_the_plan_page_shows_what_was_asked(tmp_path: Path) -> None:
    async with site(tmp_path) as (r, _, client):
        headers = await login(client, "admin")
        await client.put(f"/api/v1/levers/{OFFSET}/mode", json={"mode": "shadow"}, headers=headers)
        result = await r.executor.act(
            OFFSET, "set", {"value": 2}, who="planner", why="cheap hours: warming"
        )
        assert result.outcome == "shadowed"
        changes = (await client.get("/api/v1/plan/changes")).json()
        assert [(c["lever"], c["outcome"], c["why"]) for c in changes] == [
            (OFFSET, "shadowed", "cheap hours: warming")
        ]
        page = await client.get("/plan")
        assert page.status_code == 200
        assert "shadow: nothing was changed" in page.text
        assert "cheap hours: warming" in page.text


async def test_the_household_sees_the_plan(tmp_path: Path) -> None:
    async with site(tmp_path) as (_, _, client):
        await login(client, "kid")
        assert (await client.get("/plan")).status_code == 200
