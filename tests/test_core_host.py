import asyncio
import json
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from capfake import POINTS, FakePump

from thermaestro.cap import Message, Send, pair, serve
from thermaestro.cap.messages import Described, DeviceEvent, Subscribe
from thermaestro.core import AuditLog, Key, PluginContext, PluginHost, State, Values
from thermaestro.store import Database, Plugin, SecretStore

FAST = {"backoff_s": (0.01, 0.05), "timeout_s": 2.0}


@pytest.fixture
async def db(tmp_path: Path) -> AsyncIterator[Database]:
    async with await Database.open(tmp_path / "t.db") as d:
        yield d


def host(db: Database, tmp_path: Path, factories: dict[str, Callable[..., object]]) -> PluginHost:
    return PluginHost(
        db=db,
        secrets=SecretStore(tmp_path / "secrets.json"),
        values=Values(db),
        audit=AuditLog(tmp_path / "audit"),
        factories=factories,  # type: ignore[arg-type]
        **FAST,  # type: ignore[arg-type]
    )


def state(h: PluginHost, instance: str) -> State:
    """An instance's state as it is now (the host changes it while a test waits)."""
    return h.instances[instance].state


async def until(condition: Callable[[], bool], timeout: float = 5.0) -> None:
    async with asyncio.timeout(timeout):
        while not condition():
            await asyncio.sleep(0.02)


async def test_an_instance_runs_and_its_values_arrive(db: Database, tmp_path: Path) -> None:
    await db.put(Plugin(plugin="fake"), "pump")
    pumps: list[FakePump] = []

    def factory(context: PluginContext) -> FakePump:
        assert context.instance == "pump"
        pumps.append(FakePump())
        return pumps[-1]

    h = host(db, tmp_path, {"fake": factory})
    await h.start()
    try:
        instance = h.instances["pump"]
        await until(lambda: Key("pump", "hp1/outdoor.temp") in h.values.latest)
        assert instance.state is State.UP
        assert instance.hello is not None
        assert instance.hello.plugin == "fake"
        assert instance.described is not None
        assert len(instance.described.points) == 5
        kept = await db.run(
            lambda t: t.execute("SELECT body FROM described WHERE instance = 'pump'").fetchone()
        )
        assert kept is not None  # kept for an export
        assert len(json.loads(kept[0])["points"]) == 5
        await until(lambda: instance.health is not None)
    finally:
        await h.stop()
    assert pumps[0].acts == []  # starting a plugin acts on no lever
    assert state(h, "pump") is State.STOPPED


async def test_a_failing_plugin_is_restarted_and_the_others_run_on(
    db: Database, tmp_path: Path
) -> None:
    await db.put(Plugin(plugin="fake"), "good")
    await db.put(Plugin(plugin="broken"), "bad")
    attempts = 0

    def broken(context: PluginContext) -> FakePump:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise RuntimeError("no gateway")
        return FakePump()

    h = host(db, tmp_path, {"fake": lambda c: FakePump(), "broken": broken})
    await h.start()
    try:
        await until(lambda: Key("good", "hp1/outdoor.temp") in h.values.latest)
        await until(lambda: h.instances["bad"].state is State.UP)
        assert attempts == 3
        assert h.instances["bad"].last_error == "RuntimeError: no gateway"
    finally:
        await h.stop()
    audit = (tmp_path / "audit" / "audit.jsonl").read_text()
    assert audit.count('"plugin.restart"') == 2


class Dropped(FakePump):
    """A plugin whose connection to its device drops after it started: its events fail."""

    def __init__(self, drop: asyncio.Event) -> None:
        super().__init__()
        self.drop = drop

    async def events(self, send: Send) -> None:
        await super().events(send)
        await self.drop.wait()
        raise ConnectionError("the device closed the connection")


async def test_a_plugin_whose_events_fail_is_started_again(db: Database, tmp_path: Path) -> None:
    await db.put(Plugin(plugin="dropping"), "ha")
    drop = asyncio.Event()
    made: list[FakePump] = []

    def factory(context: PluginContext) -> FakePump:
        made.append(Dropped(drop) if not made else FakePump())
        return made[-1]

    h = host(db, tmp_path, {"dropping": factory})
    await h.start()
    try:
        await until(lambda: state(h, "ha") is State.UP)
        drop.set()
        await until(lambda: len(made) == 2 and state(h, "ha") is State.UP)
        assert h.instances["ha"].last_error == "the plugin ended its connection"
    finally:
        await h.stop()
    audit = (tmp_path / "audit" / "audit.jsonl").read_text()
    assert audit.count('"plugin.restart"') == 1


class Unsubscribed(FakePump):
    """A plugin whose point subscription fails once it started."""

    async def handle(self, request: Message, send: Send) -> None:
        if isinstance(request, Subscribe):
            raise RuntimeError("its subscription broke")
        await super().handle(request, send)


async def test_a_plugin_whose_subscription_fails_is_started_again(
    db: Database, tmp_path: Path
) -> None:
    await db.put(Plugin(plugin="breaking"), "ha")
    made: list[FakePump] = []

    def factory(context: PluginContext) -> FakePump:
        made.append(Unsubscribed() if not made else FakePump())
        return made[-1]

    h = host(db, tmp_path, {"breaking": factory})
    await h.start()
    try:
        await until(lambda: Key("ha", "hp1/outdoor.temp") in h.values.latest)
        assert len(made) == 2
    finally:
        await h.stop()


async def test_disabled_and_out_of_process_instances(db: Database, tmp_path: Path) -> None:
    await db.put(Plugin(plugin="fake", enabled=False), "off")
    await db.put(Plugin(plugin="fake"), "remote")
    h = host(db, tmp_path, {})
    await h.start()
    try:
        assert "off" not in h.instances
        assert state(h, "remote") is State.WAITING
        core, plugin_side = pair()
        served = asyncio.create_task(serve(plugin_side, FakePump()))
        attached = asyncio.create_task(h.attach(core))
        await until(lambda: Key("remote", "hp1/outdoor.temp") in h.values.latest)
        assert state(h, "remote") is State.UP
        await plugin_side.close()
        await attached
        assert state(h, "remote") is State.WAITING
        served.cancel()
    finally:
        await h.stop()


async def test_a_plugin_nobody_waits_for_is_turned_away(db: Database, tmp_path: Path) -> None:
    h = host(db, tmp_path, {})
    await h.start()
    core, plugin_side = pair()
    served = asyncio.create_task(serve(plugin_side, FakePump()))
    await asyncio.wait_for(h.attach(core), 5)
    served.cancel()
    await h.stop()


class FullDisk(AuditLog):
    """An audit log that can't write a restart."""

    async def record(self, who: str, what: str, **more: Any) -> None:
        if what == "plugin.restart":
            raise OSError("No space left on device")
        await super().record(who, what, **more)


async def test_a_failed_audit_doesnt_stop_the_restarts(db: Database, tmp_path: Path) -> None:
    await db.put(Plugin(plugin="broken"), "bad")
    attempts = 0

    def broken(context: PluginContext) -> FakePump:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise RuntimeError("no gateway")
        return FakePump()

    h = PluginHost(
        db=db,
        secrets=SecretStore(tmp_path / "secrets.json"),
        values=Values(db),
        audit=FullDisk(tmp_path / "audit"),
        factories={"broken": broken},
        **FAST,  # type: ignore[arg-type]
    )
    await h.start()
    try:
        await until(lambda: state(h, "bad") is State.UP)
        assert attempts == 3
    finally:
        await h.stop()


class Warned(Dropped):
    """A plugin that says a warning is on, then loses its device."""

    async def events(self, send: Send) -> None:
        warning = DeviceEvent(
            t=datetime.now(UTC), unit="hp1", code="brine.out.low", text="Brine out is low"
        )
        await send(warning)
        await super().events(send)


async def test_a_plugin_started_again_tells_what_is_on(db: Database, tmp_path: Path) -> None:
    """A warning the plugin raised isn't left on after it started again: the new one says
    what is still on."""
    await db.put(Plugin(plugin="warning"), "pump")
    drop = asyncio.Event()
    made: list[FakePump] = []

    def factory(context: PluginContext) -> FakePump:
        made.append(Warned(drop) if not made else FakePump())
        return made[-1]

    h = host(db, tmp_path, {"warning": factory})
    came_up: list[str] = []
    h.on_up.append(came_up.append)
    await h.start()
    try:
        await until(lambda: bool(h.instances["pump"].active) and state(h, "pump") is State.UP)
        drop.set()
        await until(lambda: len(made) == 2 and state(h, "pump") is State.UP)
        assert h.instances["pump"].active == {}
        assert came_up == ["pump", "pump"]
    finally:
        await h.stop()


class Grown(FakePump):
    """A plugin that describes a point only after it started."""

    def describe(self, id: int) -> Described:
        whole = super().describe(id)
        return whole.model_copy(
            update={"points": tuple(p for p in whole.points if p.path != "hp1/outdoor.temp")}
        )

    async def events(self, send: Send) -> None:
        await super().events(send)
        await asyncio.sleep(0.3)
        outdoor = next(p for p in POINTS if p.path == "hp1/outdoor.temp")
        await send(Described(complete=False, points=(outdoor,)))


async def test_a_point_described_later_is_followed(db: Database, tmp_path: Path) -> None:
    await db.put(Plugin(plugin="growing"), "pump")
    h = host(db, tmp_path, {"growing": lambda c: Grown()})
    await h.start()
    try:
        await until(lambda: Key("pump", "hp1/outdoor.temp") in h.values.latest)
    finally:
        await h.stop()
