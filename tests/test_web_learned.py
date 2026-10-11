"""What Thermaestro has learned, and which inputs work, on its page and over the API."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import httpx
from test_web import ADMIN_PASSWORD, FAST, KEY, ORIGIN, form
from test_web_control import login

from thermaestro.auth import Accounts, AddressLimiter, SetupCode
from thermaestro.core import AuditLog, Values
from thermaestro.core.values import Key
from thermaestro.learn import Learned, Learning
from thermaestro.learn.inputs import Check
from thermaestro.store import Database, SecretStore
from thermaestro.web import Services, create_app

CHARGE = "tank.charge_s@dev:hp1/dhw"
METER = Key("dev", "hp1/heat.produced")


@asynccontextmanager
async def site(tmp_path: Path) -> AsyncIterator[tuple[Learning, httpx.AsyncClient]]:
    async with await Database.open(tmp_path / "t.db") as db:
        audit = AuditLog(tmp_path / "audit")
        values = Values(db)
        learning = Learning(db, values, cast(Any, SimpleNamespace(instances={})), audit)
        await learning.load()
        await learning.learnings.put(
            Learned(
                name="tank.charge_s",
                scope="dev:hp1/dhw",
                kind="observed",
                value=3000.0,
                unit="s",
                state="learning",
                why="The mean of the charges seen, the newest weighing most.",
                n=4,
            )
        )
        stuck = "Always %(value)s in the last week."
        learning.checks = {
            METER: Check("dev", METER.point, "not_usable", stuck, 0.0, {"value": 348.3})
        }
        accounts = Accounts(db, audit, hasher=FAST, limiter=AddressLimiter(tries=1000))
        services = Services(
            accounts=accounts,
            db=db,
            values=values,
            host=None,
            audit=audit,
            secrets=SecretStore(tmp_path / "secrets.json"),
            setup=SetupCode(tmp_path / "setup-code"),
            learning=learning,
        )
        await accounts.create_user("admin", ADMIN_PASSWORD, ["Administrators"], by="cli")
        await accounts.create_user("kid", ADMIN_PASSWORD, ["Household"], by="cli")
        app = create_app(services, KEY)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=ORIGIN, headers={"origin": ORIGIN}
        ) as client:
            yield learning, client


async def test_the_page_shows_the_inputs_and_what_was_learned(tmp_path: Path) -> None:
    async with site(tmp_path) as (_, client):
        await login(client, "admin")
        page = (await client.get("/setup/learned")).text
        assert "Not usable on this pump" in page
        assert "Always 348.3 in the last week." in page
        assert "How long a hot-water charge takes" in page
        assert "50 min" in page
        assert "from 4 observations" in page
        client.cookies.set("lang", "sv")
        swedish = (await client.get("/setup/learned")).text
        assert "Inte användbar på den här pumpen" in swedish
        assert "348,3" in swedish


async def test_the_api_and_the_households_last_word(tmp_path: Path) -> None:
    async with site(tmp_path) as (learning, client):
        headers = await login(client, "admin")
        found = (await client.get("/api/v1/learned")).json()
        assert [i["verdict"] for i in found["inputs"]] == ["not_usable"]
        assert found["inputs"][0]["args"] == {"value": 348.3}
        [value] = found["values"]
        assert (value["key"], value["value"], value["used"]) == (CHARGE, 3000.0, 3000.0)
        pinned = await client.put(
            "/api/v1/learned/pin", json={"key": CHARGE, "value": 2400}, headers=headers
        )
        assert pinned.json()["used"] == 2400.0
        over = await client.post(
            "/api/v1/learned/start-over", json={"key": CHARGE}, headers=headers
        )
        assert (over.json()["value"], over.json()["pinned"]) == (None, 2400.0)
        missing = await client.post(
            "/api/v1/learned/start-over", json={"key": "nothing@here"}, headers=headers
        )
        assert missing.status_code == 400
        checked = await client.post("/api/v1/learned/inputs/check", headers=headers)
        assert checked.json() == []  # no device here
        assert learning.checks == {}


async def test_a_pin_is_typed_in_minutes_on_the_page(tmp_path: Path) -> None:
    async with site(tmp_path) as (learning, client):
        await login(client, "admin")
        answer = await form(
            client,
            "/setup/learned",
            "/setup/learned/pin",
            key=CHARGE,
            value="45",
            **{"in": "minutes"},
        )
        assert answer.status_code == 303
        assert learning.learnings.used("tank.charge_s", "dev:hp1/dhw") == 2700.0
        await form(client, "/setup/learned", "/setup/learned/pin", key=CHARGE, value="")
        assert learning.learnings.used("tank.charge_s", "dev:hp1/dhw") == 3000.0
        wrong = await form(client, "/setup/learned", "/setup/learned/pin", key=CHARGE, value="soon")
        assert wrong.status_code == 400


async def test_the_household_sees_but_doesnt_change(tmp_path: Path) -> None:
    async with site(tmp_path) as (_, client):
        headers = await login(client, "kid")
        assert (await client.get("/api/v1/learned")).status_code == 403
        pin = await client.put(
            "/api/v1/learned/pin", json={"key": CHARGE, "value": 60}, headers=headers
        )
        assert pin.status_code == 403
