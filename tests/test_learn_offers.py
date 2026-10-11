"""Learned hot-water times offered as standing intents, and NibePi's hot-water profile as
the tank's starting week."""

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import date, time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from zoneinfo import ZoneInfo

import httpx
import pytest
from test_web import ADMIN_PASSWORD, FAST, KEY, ORIGIN
from test_web_control import login

from thermaestro.auth import Accounts, AddressLimiter, SetupCode
from thermaestro.core import AuditLog, Values
from thermaestro.intents import Capabilities, Intents
from thermaestro.learn import Learned, Learning
from thermaestro.learn.offers import offers, peaks
from thermaestro.learn.tank import Demand
from thermaestro.migrate import profile
from thermaestro.store import Database, SecretStore
from thermaestro.web import Services, create_app

TANK = "dev:hp1/dhw"


def week_with(peaks_at: dict[int, tuple[int, float]], seen: int = 3) -> Demand:
    week = Demand(seen=[seen] * 7)
    for day, (hour, drawn) in peaks_at.items():
        week.hours[day * 24 + hour] = drawn
        week.hours[day * 24 + hour + 1] = drawn / 4
    return week


def test_the_peaks_of_each_day() -> None:
    week = week_with({0: (7, 3.0), 5: (9, 2.0)})
    week.hours[2 * 24 + 7] = 0.5  # Wednesday: too little to be worth a deadline
    assert peaks(week) == {0: [7], 5: [9]}
    rarely = week_with({0: (7, 3.0)}, seen=1)
    assert peaks(rarely) == {}


def test_offers_by_hour_left_out_where_covered_or_turned_down() -> None:
    week = week_with({d: (7, 3.0) for d in range(5)} | {5: (9, 2.0), 6: (9, 2.0)})
    found = offers(TANK, week, set(), set(), level="hot", temp=None)
    assert [(o.by, o.days) for o in found] == [(time(7), (0, 1, 2, 3, 4)), (time(9), (5, 6))]
    assert found[0].request() == {
        "kind": "hot_water_by",
        "scope": TANK,
        "deadlines": [{"days": [0, 1, 2, 3, 4], "by": "07:00", "level": "hot"}],
        "strength": "should",
    }
    covered = offers(TANK, week, {(0, 6)}, set(), level=None, temp=50.0)  # 06:00 Monday
    assert covered[0].days == (1, 2, 3, 4)
    assert covered[0].request()["deadlines"][0]["temp"] == 50.0
    turned_down = offers(TANK, week, set(), {found[1].id}, level="hot", temp=None)
    assert [o.by for o in turned_down] == [time(7)]


def text(hours: list[float]) -> str:
    return json.dumps({"profile": hours, "tempPlan": [], "modePlan": [], "meta": {}})


def test_nibepis_profile_is_read_and_moved_into_the_house_zone() -> None:
    hours = [0.0] * 168
    hours[7] = 4.0  # Monday 07:00 as NibePi kept it
    week = profile.week(
        profile.read(text(hours)), ZoneInfo("UTC"), ZoneInfo("Europe/Stockholm"), date(2026, 1, 14)
    )
    assert week.hours[8] == 2.0  # 08:00 in Stockholm in winter, halved into the tank's degrees
    assert week.seen == [profile.SEEN] * 7
    summer = profile.week(hours, ZoneInfo("UTC"), ZoneInfo("Europe/Stockholm"), date(2026, 7, 1))
    assert summer.hours[9] == 2.0
    with pytest.raises(profile.NotProfile, match="168 hours"):
        profile.read(json.dumps({"profile": [1, 2]}))
    with pytest.raises(profile.NotProfile, match="below 0"):
        profile.read(text([-1.0] + [0.0] * 167))
    with pytest.raises(profile.NotProfile, match="not JSON"):
        profile.read("{")


@asynccontextmanager
async def site(tmp_path: Path) -> AsyncIterator[tuple[Learning, Intents, httpx.AsyncClient]]:
    node = SimpleNamespace
    described = node(
        nodes=[node(path="hp1", kind="unit"), node(path="hp1/dhw", kind="dhw_tank")],
        points=[],
    )
    host = cast(Any, node(instances={"dev": node(described=described)}))
    async with await Database.open(tmp_path / "t.db") as db:
        audit = AuditLog(tmp_path / "audit")
        values = Values(db)
        learning = Learning(db, values, host, audit)
        await learning.load()
        caps = Capabilities(tanks=frozenset({TANK}))
        intents = Intents(db, audit, capabilities=lambda: caps)
        accounts = Accounts(db, audit, hasher=FAST, limiter=AddressLimiter(tries=1000))
        services = Services(
            accounts=accounts,
            db=db,
            values=values,
            host=None,
            audit=audit,
            secrets=SecretStore(tmp_path / "secrets.json"),
            setup=SetupCode(tmp_path / "setup-code"),
            intents=intents,
            learning=learning,
            zone=ZoneInfo("Europe/Stockholm"),
        )
        await accounts.create_user("admin", ADMIN_PASSWORD, ["Administrators"], by="cli")
        app = create_app(services, KEY)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=ORIGIN, headers={"origin": ORIGIN}
        ) as client:
            yield learning, intents, client


async def test_an_offer_is_asked_for_or_turned_down(tmp_path: Path) -> None:
    async with site(tmp_path) as (learning, intents, client):
        week = week_with({d: (7, 3.0) for d in range(5)} | {5: (9, 2.0), 6: (9, 2.0)})
        unproved = Learned(name="tank.demand", scope=TANK, kind="model", value=week.to_value())
        await learning.learnings.put(unproved)
        headers = await login(client, "admin")
        assert (await client.get("/api/v1/learned")).json()["offers"] == []  # not proved
        await learning.learnings.put(unproved.model_copy(update={"state": "proved"}))
        found = (await client.get("/api/v1/learned")).json()["offers"]
        assert [o["by"] for o in found] == ["07:00", "09:00"]
        assert "Hot water at 50 °C by" in found[0]["text"]
        asked = await client.post(
            "/api/v1/learned/offers/accept", json={"id": found[0]["id"]}, headers=headers
        )
        assert asked.status_code == 200, asked.text
        [intent] = await intents.all()
        assert (intent.kind, intent.scope, intent.principal) == ("hot_water_by", TANK, "user:admin")
        left = (await client.get("/api/v1/learned")).json()["offers"]
        assert [o["by"] for o in left] == ["09:00"]  # 07:00 is covered now
        gone = await client.post(
            "/api/v1/learned/offers/dismiss", json={"id": left[0]["id"]}, headers=headers
        )
        assert gone.status_code == 200
        assert (await client.get("/api/v1/learned")).json()["offers"] == []
        again = await client.post(
            "/api/v1/learned/offers/accept", json={"id": left[0]["id"]}, headers=headers
        )
        assert again.status_code == 400


async def test_the_week_starts_from_nibepis_profile(tmp_path: Path) -> None:
    async with site(tmp_path) as (learning, _, client):
        headers = await login(client, "admin")
        hours = [0.0] * 168
        hours[7] = 4.0
        read = await client.post(
            "/api/v1/import/nibepi/profile/read",
            json={"profile": text(hours), "timezone": "UTC"},
            headers=headers,
        )
        assert read.status_code == 200, read.text
        preview = read.json()
        assert (preview["tank"], preview["empty"]) == (TANK, False)
        assert learning.learnings.get("tank.demand", TANK) is None  # nothing changes yet
        applied = await client.post(
            f"/api/v1/import/nibepi/profile/{preview['token']}", headers=headers
        )
        assert applied.status_code == 200, applied.text
        found = learning.learnings.get("tank.demand", TANK)
        assert found is not None
        assert (
            Demand.from_value(found.value).day(0)[8] == 2.0
            or Demand.from_value(found.value).day(0)[9] == 2.0
        )
        assert (found.n, found.state) == (0, "learning")
        await learning.learnings.put(found.model_copy(update={"n": 3}))
        again = await client.post(
            "/api/v1/import/nibepi/profile/read",
            json={"profile": text(hours), "timezone": "UTC"},
            headers=headers,
        )
        refused = await client.post(
            f"/api/v1/import/nibepi/profile/{again.json()['token']}", headers=headers
        )
        assert refused.status_code == 400
        assert "start it over" in refused.json()["error"]
        empty = await client.post(
            "/api/v1/import/nibepi/profile/read",
            json={"profile": text([0.0] * 168)},
            headers=headers,
        )
        assert empty.json()["empty"] is True


async def test_the_pages_show_the_offer_and_the_profile(tmp_path: Path) -> None:
    async with site(tmp_path) as (learning, _, client):
        week = week_with({d: (7, 3.0) for d in range(5)})
        await learning.learnings.put(
            Learned(
                name="tank.demand", scope=TANK, kind="model", value=week.to_value(), state="proved"
            )
        )
        await login(client, "admin")
        learned = (await client.get("/setup/learned")).text
        assert "Suggested from the week" in learned
        assert "Ask for it" in learned
        nibepi = (await client.get("/setup/nibepi")).text
        assert "NibePi&#39;s hot-water profile" in nibepi or "NibePi's hot-water profile" in nibepi
