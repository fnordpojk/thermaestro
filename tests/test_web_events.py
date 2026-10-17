"""Events on their page and over the API: the household's for the household, the house's
workings only for those who may read the settings, each in its reader's language."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import httpx
from test_web import ADMIN_PASSWORD, FAST, KEY, ORIGIN
from test_web_control import login

from thermaestro import clock
from thermaestro.auth import Accounts, AddressLimiter, SetupCode
from thermaestro.core import AuditLog, Values
from thermaestro.core.events import Events, Raise
from thermaestro.planner.watch import COMFORT_MISSED, FLOOR_BROKEN
from thermaestro.store import Database, SecretStore
from thermaestro.web import Services, create_app


@asynccontextmanager
async def site(tmp_path: Path) -> AsyncIterator[tuple[Events, httpx.AsyncClient]]:
    async with await Database.open(tmp_path / "t.db") as db:
        audit = AuditLog(tmp_path / "audit")
        events = Events(db)
        accounts = Accounts(db, audit, hasher=FAST, limiter=AddressLimiter(tries=1000))
        services = Services(
            accounts=accounts,
            db=db,
            values=Values(db),
            host=None,
            audit=audit,
            secrets=SecretStore(tmp_path / "secrets.json"),
            setup=SetupCode(tmp_path / "setup-code"),
            planner=cast(Any, SimpleNamespace(events=events)),
        )
        await accounts.create_user("admin", ADMIN_PASSWORD, ["Administrators"], by="cli")
        await accounts.create_user("kid", ADMIN_PASSWORD, ["Household"], by="cli")
        app = create_app(services, KEY)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=ORIGIN, headers={"origin": ORIGIN}
        ) as client:
            yield events, client


async def raised(events: Events) -> None:
    now = clock.time()
    floor = Raise("floor.broken", "pump:hp1/dhw", FLOOR_BROKEN, {"floor": 42.0})
    await events.raise_("floor.broken:pump:hp1/dhw", floor, now=now - 600)
    room = Raise(
        "comfort.missed",
        "room:living",
        COMFORT_MISSED,
        {"room": "Living room", "low": 20.5},
        audience="household",
    )
    await events.moment("comfort.missed:room:living", room, now=now - 300)


async def test_the_household_sees_its_own_events(tmp_path: Path) -> None:
    async with site(tmp_path) as (events, client):
        await raised(events)
        await login(client, "kid")
        found = (await client.get("/api/v1/events")).json()
        assert [e["kind"] for e in found] == ["comfort.missed"]
        assert found[0]["text"] == "Living room has been below its 20.5 °C for over half an hour"
        page = (await client.get("/events")).text
        assert "Living room has been below its 20.5 °C for over half an hour" in page
        assert "floor" not in page


async def test_an_administrator_sees_the_house_too_in_their_language(tmp_path: Path) -> None:
    async with site(tmp_path) as (events, client):
        await raised(events)
        await login(client, "admin")
        found = (await client.get("/api/v1/events")).json()
        assert [e["kind"] for e in found] == ["floor.broken", "comfort.missed"]
        assert found[0]["cleared"] is None
        page = (await client.get("/events")).text
        assert "The hot water fell below its floor of 42.0 °C" in page
        client.cookies.set("lang", "sv")
        swedish = (await client.get("/events")).text
        assert "Varmvattnet föll under sin lägsta temperatur, 42,0 °C" in swedish
        assert "Living room har varit under sina 20,5 °C i mer än en halvtimme" in swedish
