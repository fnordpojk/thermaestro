import asyncio
from collections.abc import AsyncIterator, Callable
from pathlib import Path

import pytest
from capfake import FakePump

from thermaestro.cap import pair, serve
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
        await until(lambda: instance.health is not None)
    finally:
        await h.stop()
    assert pumps[0].acts == []  # nothing in Stage 2 acts on a lever
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
