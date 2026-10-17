"""Notifications: nothing sent unless a person turned it on, only what their rights let
them see, in their language, once per change, within the limit, and every failure kept."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import httpx
import pytest
from test_web import ADMIN_PASSWORD, FAST, KEY, ORIGIN, form
from test_web_control import login

from thermaestro.auth import Accounts, AddressLimiter, Preferences, SetupCode, User
from thermaestro.cap.messages import Notified, NotifyList, Target
from thermaestro.core import AuditLog, Values
from thermaestro.core.events import Events, Raise
from thermaestro.planner.watch import COMFORT_MISSED, FLOOR_BROKEN
from thermaestro.store import Database, SecretStore
from thermaestro.web import Services, create_app
from thermaestro.web.notifications import HomeAssistantTarget, Notifier, Wanted, allowed

PHONE = HomeAssistantTarget(instance="ha", target="mobile_app_pixel_8")
ROOM = Raise(
    "comfort.missed",
    "room:living",
    COMFORT_MISSED,
    {"room": "Living room", "low": 20.5},
    audience="household",
)
FLOOR = Raise("floor.broken", "pump:hp1/dhw", FLOOR_BROKEN, {"floor": 42.0})


def at_risk(principal: str) -> Raise:
    return Raise(
        "intent.at_risk",
        "pump:hp1/dhw",
        "The hot water is planned %(short)s °C short at %(at)s",
        {"short": 4.0, "at": "19:30"},
        audience="household",
        principal=principal,
    )


@dataclass
class Link:
    """A Home Assistant plugin's side: what it was asked to send."""

    sent: list[tuple[str, str, str]] = field(default_factory=list)
    refuse: str | None = None

    async def notify_targets(self, *, timeout: float) -> NotifyList:
        phone = Target(id="mobile_app_pixel_8", name="Send a notification via Pixel 8")
        return NotifyList(id=1, targets=(phone,))

    async def notify(self, target: str, title: str, message: str, *, timeout: float) -> Notified:
        self.sent.append((target, title, message))
        return Notified(id=1, ok=self.refuse is None, detail=self.refuse)


@dataclass
class Broker:
    """The discovery publisher's side."""

    on: bool = True
    sent: list[tuple[str, dict[str, Any]]] = field(default_factory=list)

    def notify_topic(self, user: str) -> str | None:
        return f"thermaestro/abcd/notify/{user}" if self.on else None

    async def notify(self, user: str, payload: dict[str, Any]) -> str | None:
        if not self.on:
            return "Home Assistant discovery over MQTT is off"
        self.sent.append((user, payload))
        return None


@dataclass
class House:
    db: Database
    host: Any
    notifier: Notifier
    events: Events
    accounts: Accounts
    link: Link
    broker: Broker
    clock: list[float]

    async def user(self, name: str) -> User:
        found = await self.accounts.user(name)
        assert found is not None
        return found


@asynccontextmanager
async def house(tmp_path: Path, limit: int = 10) -> AsyncIterator[House]:
    async with await Database.open(tmp_path / "t.db") as db:
        audit = AuditLog(tmp_path / "audit")
        accounts = Accounts(db, audit, hasher=FAST, limiter=AddressLimiter(tries=1000))
        await accounts.create_user("admin", ADMIN_PASSWORD, ["Administrators"], by="cli")
        await accounts.create_user("kid", ADMIN_PASSWORD, ["Household"], by="cli")
        link, broker = Link(), Broker()
        instance = SimpleNamespace(
            setting=SimpleNamespace(plugin="homeassistant"),
            hello=SimpleNamespace(features=("subscribe", "notify")),
            link=link,
        )
        host = cast(Any, SimpleNamespace(instances={"ha": instance}))
        clock = [1_800_000_000.0]
        notifier = Notifier(
            db,
            accounts,
            host,
            cast(Any, broker),
            lambda: UTC,
            clock=lambda: clock[0],
            limit=limit,
        )
        events = Events(db)
        notifier.listen(events)
        yield House(db, host, notifier, events, accounts, link, broker, clock)


async def test_nothing_is_sent_unless_turned_on(tmp_path: Path) -> None:
    async with house(tmp_path) as h:
        await h.events.raise_("comfort.missed:room:living", ROOM, now=h.clock[0])
        await h.events.raise_("floor.broken:pump:hp1/dhw", FLOOR, now=h.clock[0])
        await h.notifier.drain()
        assert h.link.sent == []
        assert h.broker.sent == []


async def test_what_a_person_may_turn_on_follows_their_rights(tmp_path: Path) -> None:
    async with house(tmp_path) as h:
        kid, admin = await h.user("kid"), await h.user("admin")
        assert allowed(kid) == ["mine", "rooms", "summary"]
        assert allowed(admin) == ["mine", "everyone", "rooms", "summary", "house"]
        with pytest.raises(ValueError, match="house"):
            await h.notifier.choose(kid, Wanted(conditions=("mine", "house")))
        other = HomeAssistantTarget(instance="elsewhere", target="x")
        with pytest.raises(ValueError, match="no Home Assistant"):
            await h.notifier.choose(kid, Wanted(conditions=("mine",), home_assistant=other))
        assert await h.notifier.wanted(kid) == Wanted()


async def test_ones_own_requests_to_ones_phone_once_per_change(tmp_path: Path) -> None:
    async with house(tmp_path) as h:
        kid = await h.user("kid")
        await h.notifier.choose(kid, Wanted(conditions=("mine",), home_assistant=PHONE))
        now = h.clock[0]
        await h.events.raise_("intent.state:a", at_risk("user:kid"), now=now)
        await h.events.raise_("intent.state:a", at_risk("user:kid"), now=now + 60)  # the same
        await h.events.raise_("intent.state:b", at_risk("user:admin"), now=now)  # not theirs
        await h.events.raise_("floor.broken:pump:hp1/dhw", FLOOR, now=now)  # not for them
        await h.notifier.drain()
        assert h.link.sent == [
            ("mobile_app_pixel_8", "Thermaestro", "The hot water is planned 4.0 °C short at 19:30")
        ]
        await h.events.clear("intent.state:a", now=now + 120)
        await h.notifier.drain()
        assert h.link.sent[-1][2] == "Over: The hot water is planned 4.0 °C short at 19:30"
        [last, first] = await h.notifier.recent(kid)
        assert (first.channel, first.event, first.outcome) == (
            "home_assistant",
            "intent.state:a raised",
            "sent",
        )
        assert last.event == "intent.state:a cleared"


async def test_in_their_language_and_on_their_topic(tmp_path: Path) -> None:
    async with house(tmp_path) as h:
        kid = await h.user("kid")
        await h.accounts.set_preferences(kid, Preferences(language="sv"))
        await h.notifier.choose(kid, Wanted(conditions=("rooms",), mqtt=True))
        assert h.notifier.topic(kid) == "thermaestro/abcd/notify/kid"
        await h.events.moment("comfort.missed:room:living", ROOM, now=h.clock[0])
        await h.notifier.drain()
        [(user, payload)] = h.broker.sent  # a moment: once
        assert user == "kid"
        assert (
            payload["message"] == "Living room har varit under sina 20,5 °C i mer än en halvtimme"
        )
        assert (payload["kind"], payload["state"]) == ("comfort.missed", "raised")


async def test_beyond_the_limit_held_back_and_failures_kept(tmp_path: Path) -> None:
    async with house(tmp_path, limit=2) as h:
        admin = await h.user("admin")
        await h.notifier.choose(admin, Wanted(conditions=("house",), home_assistant=PHONE))
        h.link.refuse = "Service notify.mobile_app_pixel_8 not found"
        for i in range(3):
            await h.events.moment(f"plugin.missing:p{i}", FLOOR, now=h.clock[0] + i)
        await h.notifier.drain()
        found = await h.notifier.recent(admin)
        assert [s.outcome for s in reversed(found)] == ["failed", "failed", "held"]
        assert found[-1].detail == "Service notify.mobile_app_pixel_8 not found"
        h.clock[0] += 3600  # an hour on, it goes again
        h.link.refuse = None
        await h.events.moment("plugin.missing:p9", FLOOR, now=h.clock[0])
        await h.notifier.drain()
        assert (await h.notifier.recent(admin))[0].outcome == "sent"


async def test_a_test_goes_everywhere_chosen(tmp_path: Path) -> None:
    async with house(tmp_path) as h:
        kid = await h.user("kid")
        await h.notifier.choose(kid, Wanted(home_assistant=PHONE, mqtt=True))
        h.broker.on = False
        done = await h.notifier.test(kid)
        assert [(s.channel, s.outcome) for s in done] == [
            ("home_assistant", "sent"),
            ("mqtt", "failed"),
        ]
        assert done[1].detail == "Home Assistant discovery over MQTT is off"


async def test_turned_on_from_ones_own_page(tmp_path: Path) -> None:
    async with house(tmp_path) as h:
        services = Services(
            accounts=h.accounts,
            db=h.db,
            values=Values(h.db),
            host=h.host,
            audit=AuditLog(tmp_path / "web-audit"),
            secrets=SecretStore(tmp_path / "secrets.json"),
            setup=SetupCode(tmp_path / "setup-code"),
            notifier=h.notifier,
        )
        app = create_app(services, KEY)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=ORIGIN, headers={"origin": ORIGIN}
        ) as client:
            headers = await login(client, "kid")
            found = (await client.get("/api/v1/account/notifications")).json()
            assert found["allowed"] == ["mine", "rooms", "summary"]
            assert found["wanted"] == {"conditions": [], "home_assistant": None, "mqtt": False}
            assert found["targets"] == [
                {
                    "instance": "ha",
                    "target": "mobile_app_pixel_8",
                    "name": "Send a notification via Pixel 8",
                }
            ]
            refused = await client.put(
                "/api/v1/account/notifications", json={"conditions": ["house"]}, headers=headers
            )
            assert refused.status_code == 400
            page = (await client.get("/account")).text
            assert "Send a notification via Pixel 8" in page
            assert 'value="house"' not in page
            saved = await form(
                client,
                "/account",
                "/account/notifications",
                conditions="mine",
                target="ha/mobile_app_pixel_8",
            )
            assert saved.status_code == 303
            kid = await h.user("kid")
            assert await h.notifier.wanted(kid) == Wanted(
                conditions=("mine",), home_assistant=PHONE
            )
            tested = await client.post("/api/v1/account/notifications/test", headers=headers)
            assert [(s["channel"], s["outcome"]) for s in tested.json()] == [
                ("home_assistant", "sent")
            ]
            assert h.link.sent[-1][2].startswith("A test notification")
