import asyncio
import json
import os
import signal
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

from capfake import FakePump

from thermaestro.core import run
from thermaestro.store import Database, Layout, Plugin


def layout(tmp_path: Path) -> Layout:
    return Layout(tmp_path / "config", tmp_path / "state")


def audit_whats(state: Path) -> list[str]:
    path = state / "audit" / "audit.jsonl"
    if not path.exists():
        return []
    return [json.loads(line)["what"] for line in path.read_text().splitlines()]


async def test_runs_until_stopped_and_writes_history(tmp_path: Path) -> None:
    lay = layout(tmp_path)
    lay.state.mkdir(mode=0o700)
    async with await Database.open(lay.database) as db:
        await db.put(Plugin(plugin="fake"), "pump")
    stop = asyncio.Event()
    task = asyncio.create_task(
        run(lay, stop=stop, factories={"fake": lambda c: FakePump()}, flush_s=0.1)
    )
    async with asyncio.timeout(10):
        while "plugin.start" not in audit_whats(lay.state):
            await asyncio.sleep(0.05)
        await asyncio.sleep(0.5)
    stop.set()
    await asyncio.wait_for(task, 10)
    assert audit_whats(lay.state)[0] == "core.start"
    assert audit_whats(lay.state)[-1] == "core.stop"
    with closing(sqlite3.connect(lay.database)) as raw:
        points = {p for (p,) in raw.execute("SELECT DISTINCT point FROM history")}
    assert "hp1/outdoor.temp" in points


async def test_the_command_stops_cleanly_on_sigterm(tmp_path: Path) -> None:
    lay = layout(tmp_path)
    env = dict(
        os.environ,
        CONFIGURATION_DIRECTORY=str(lay.config),
        STATE_DIRECTORY=str(lay.state),
    )
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "thermaestro.cli", "run", env=env
    )
    try:
        async with asyncio.timeout(20):
            while "core.start" not in audit_whats(lay.state):
                await asyncio.sleep(0.1)
        process.send_signal(signal.SIGTERM)
        assert await asyncio.wait_for(process.wait(), 20) == 0
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
    assert audit_whats(lay.state)[-1] == "core.stop"
