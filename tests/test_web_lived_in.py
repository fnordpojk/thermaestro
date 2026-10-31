"""Every page and every read of the API, in each language, over a house that has been
lived in: a simulated pump and house over three days, the pump's connection lost for two
hours on the second, the learners run over what was recorded and the rules planning. None
may answer with a server error, and no failure may be left open as an event. And again
with every part of every page failing: each page is still shown, saying which part isn't.

Tests over an empty site show that a page renders; these show that it renders with what a
running installation has collected, the shapes only time and outages make included."""

import re
from collections.abc import Awaitable
from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute
from plant import draw
from plant.harness import PLANT, PUMP, Sim, running
from plant.planning import Planned, planning
from plant.vloop import simulate
from test_web import ADMIN_PASSWORD, FAST, KEY, ORIGIN
from test_web_control import login

from thermaestro import clock
from thermaestro.auth import Accounts, AddressLimiter, SetupCode
from thermaestro.core import AuditLog
from thermaestro.core.events import Events
from thermaestro.core.failures import PREFIX, Failures, attached_for_tests
from thermaestro.intents import Level, kinds
from thermaestro.learn import Learning
from thermaestro.learn.records import COMPRESSOR_DAY, CompressorDay, Gap, read
from thermaestro.store import Plugin, SecretStore
from thermaestro.web import Services, create_app
from thermaestro.web.failing import Parts
from thermaestro.web.setup_pages import TOPICS

DAY_S = 86_400.0
PARAMS = {
    "instance": PUMP,
    "point": "hp1/outdoor.temp",
    "id": "none",
    "raw": "none",
    "mode": "auto",
    "code": "en",
    "token": "none",
}
"""A value for each path parameter; one that names nothing must be refused, not fail."""
LANGUAGES = ("en", "sv", "de")


def _paths(app: FastAPI) -> list[str]:
    out = []
    for route in app.routes:
        if not isinstance(route, APIRoute) or "GET" not in (route.methods or ()):
            continue
        if route.path == "/setup/{topic}":
            out += [f"/setup/{t}" for t in TOPICS]
            continue
        names = re.findall(r"{(\w+)(?::\w+)?}", route.path)
        missing = [n for n in names if n not in PARAMS]
        assert not missing, f"no value for {missing} in {route.path}: add one to PARAMS"
        out.append(re.sub(r"{(\w+)(?::\w+)?}", lambda m: PARAMS[m.group(1)], route.path))
    return sorted(set(out))


async def _lived_in(
    tmp_path: Path, sim: Sim, planned: Planned, events: Events, failures: Failures
) -> list[str]:
    """Every page and read over the house as it is now: what answered with a server error,
    and the failures left open."""
    audit = AuditLog(tmp_path / "web-audit")
    learning = Learning(sim.db, sim.values, sim.host, audit)
    await learning.load()
    await learning.check_inputs()
    await learning.learn()
    days = [d for _, d in await read(sim.db, "pump:hp1", COMPRESSOR_DAY)]
    # The house has both kinds of day, or this shows less than it says.
    assert any(isinstance(d, Gap) for d in days), days
    assert any(isinstance(d, CompressorDay) for d in days), days

    accounts = Accounts(sim.db, audit, hasher=FAST, limiter=AddressLimiter(tries=1000))
    await accounts.create_user("admin", ADMIN_PASSWORD, ["Administrators"], by="cli")
    services = Services(
        accounts=accounts,
        db=sim.db,
        values=sim.values,
        host=sim.host,
        audit=audit,
        secrets=SecretStore(tmp_path / "web-secrets.json"),
        setup=SetupCode(tmp_path / "setup-code"),
        sensors=planned.hub,
        executor=sim.executor,
        planner=planned.planner,
        learning=learning,
        intents=planned.intents,
    )
    app = create_app(services, KEY)
    wrong: list[str] = []
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url=ORIGIN, headers={"origin": ORIGIN}
    ) as client:
        await login(client, "admin")
        for path in _paths(app):
            for language in LANGUAGES:
                answer = await client.get(path, headers={"accept-language": language})
                # `/ready` says it isn't ready to control with a 503: its answer, no error.
                if answer.status_code >= 500 and (path, answer.status_code) != ("/ready", 503):
                    wrong.append(f"{path} ({language}): {answer.status_code}")
    await failures.settle()
    return wrong + [e.text() for e in await events.open(PREFIX)]


def test_every_page_and_read_over_a_lived_in_house(tmp_path: Path) -> None:
    s = draw(11, emitter="radiators")

    async def main() -> list[str]:
        sim = Sim(s, tmp_path, {"cs1/heating.offset": "shadow", "dhw/block": "shadow"})
        async with running(sim):
            # Every failure from here on is an event: the planner's, the learners', the pages'.
            events = Events(sim.db)
            with attached_for_tests(events) as failures:
                made = {"principal": "user:admin", "created": clock.now() - timedelta(minutes=1)}
                household = [
                    kinds.hot_water_floor("pump:hp1/dhw", 42.0, **made),  # type: ignore[arg-type]
                    kinds.comfort_band("pump:hp1/cs1", [("day", ((), None, None))], **made),  # type: ignore[arg-type]
                ]
                levels = [Level(id="day", name="Day", scope="pump:hp1/cs1", low=20.5, high=22.0)]
                async with planning(sim, household, levels, []) as planned:
                    await sim.run(1.25)
                    # Two hours with nothing read: the pump and the house's sensors off.
                    kept = {id: await sim.db.get(Plugin, id) for id in (PUMP, PLANT)}
                    for id, plugin in kept.items():
                        assert plugin is not None
                        await sim.db.put(plugin.model_copy(update={"enabled": False}), id)
                        await sim.host.apply(id)
                    await sim.advance(2 * 3600)
                    for id, plugin in kept.items():
                        assert plugin is not None
                        await sim.db.put(plugin, id)
                        await sim.host.apply(id)
                    await sim.run(3.1 - (clock.time() - s.start_time.timestamp()) / DAY_S)
                    return await _lived_in(tmp_path, sim, planned, events, failures)

    assert simulate(main, s.start_time) == []


async def test_a_page_whose_parts_all_fail_is_still_shown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each part's stand-in renders: with every part failing, every page answers, and one
    built of parts says which couldn't be shown."""
    from test_web_learned import site

    original = Parts.get

    async def failing(
        self: Parts, name: str, label: str, build: Awaitable[Any], default: Any
    ) -> Any:
        build.close()  # type: ignore[attr-defined]  # a coroutine, never awaited

        async def broken() -> Any:
            raise RuntimeError(f"{name} failed, for the test")

        return await original(self, name, label, broken(), default)

    monkeypatch.setattr(Parts, "get", failing)
    async with site(tmp_path) as (_, client):
        await login(client, "admin")
        for path in ("/", "/events", "/system/health", "/setup/learned"):
            for language in LANGUAGES:
                answer = await client.get(path, headers={"accept-language": language})
                assert answer.status_code == 200, (path, language, answer.text[:300])
                assert 'class="error" role="alert"' in answer.text, (path, language)


async def test_an_unexpected_error_is_answered_and_raised_as_an_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from test_web_learned import site

    from thermaestro.store import Database
    from thermaestro.web.learn_operations import LearnOperations

    async def broken(self: Any, caller: Any) -> Any:
        raise KeyError("draws")

    async with await Database.open(tmp_path / "events.db") as db:
        events = Events(db)
        with attached_for_tests(events) as failures:
            async with site(tmp_path) as (_, client):
                headers = await login(client, "admin")
                monkeypatch.setattr(LearnOperations, "learned", broken)
                answer = await client.get("/api/v1/learned", headers=headers)
                assert answer.status_code == 500
                body = answer.json()
                assert body["error"] == "something went wrong"
                assert "draws" not in answer.text  # what it said is for the Events page
                await failures.settle()
                [open_] = await events.open(PREFIX)
                assert open_.args["id"] == body["id"]
                assert open_.args["error"] == "KeyError: 'draws'"
                assert open_.args["where"] == "/api/v1/learned"
                monkeypatch.undo()
                assert (await client.get("/api/v1/learned", headers=headers)).status_code == 200
                await failures.settle()
                assert await events.open(PREFIX) == []  # it works again: cleared
