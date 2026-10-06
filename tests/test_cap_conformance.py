"""The conformance suite against the fake plugin: the same plugin, unchanged, in process
and in a process of its own over each socket."""

import asyncio
import contextlib
import shutil
import sys
import tempfile
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
from capfake import FakePump

from thermaestro.cap import Endpoint, pair, serve
from thermaestro.cap.conformance import Finding, Try, run
from thermaestro.cap.sockets import OnPlugin, listen_tcp, listen_unix

FAKE = Path(__file__).parent / "capfake.py"
TRIES = (Try("hp1/dhw/block", "engage"), Try("hp1/cs1/heating.offset", "set", {"value": -2}))
FAST = {"timeout_s": 3.0, "quiet_s": 0.3}


@pytest.fixture
def short_dir() -> Iterator[Path]:
    d = Path(tempfile.mkdtemp(prefix="cap", dir="/tmp"))
    yield d
    shutil.rmtree(d)


async def in_process(pump: FakePump) -> list[Finding]:
    core, plugin = pair()
    task = asyncio.create_task(serve(plugin, pump))
    try:
        return await run(core, tries=TRIES, **FAST)
    finally:
        await plugin.close()
        await task


async def test_conforms_in_process() -> None:
    pump = FakePump()
    assert await in_process(pump) == []
    assert [(a.lever, a.op) for a in pump.acts] == [
        ("conformance.missing/x.conformance.none", "set"),
        ("hp1/dhw/block", "engage"),
        ("hp1/cs1/heating.offset", "set"),
    ]


@contextlib.asynccontextmanager
async def plugin_process(*args: str) -> AsyncIterator[asyncio.subprocess.Process]:
    process = await asyncio.create_subprocess_exec(sys.executable, str(FAKE), *args)
    try:
        yield process
    finally:
        if process.returncode is None:
            process.terminate()
        await process.wait()


def first_plugin(findings: asyncio.Future[list[Finding]]) -> OnPlugin:
    async def on_plugin(endpoint: Endpoint) -> None:
        if not findings.done():
            findings.set_result(await run(endpoint, tries=TRIES, **FAST))

    return on_plugin


async def test_conforms_over_a_unix_socket_from_its_own_process(short_dir: Path) -> None:
    findings: asyncio.Future[list[Finding]] = asyncio.get_running_loop().create_future()
    path = short_dir / "plugins" / "s"
    server = await listen_unix(path, first_plugin(findings))
    async with server, plugin_process("--unix", str(path)):
        assert await asyncio.wait_for(findings, 30) == []


async def test_conforms_over_tcp_from_its_own_process(short_dir: Path) -> None:
    findings: asyncio.Future[list[Finding]] = asyncio.get_running_loop().create_future()
    connection_file = short_dir / "plugins" / "connection.json"
    server = await listen_tcp(connection_file, first_plugin(findings))
    async with server, plugin_process("--tcp", str(connection_file)):
        assert await asyncio.wait_for(findings, 30) == []


@pytest.mark.parametrize(
    ("flaw", "check"),
    [
        ("wrong-unit", "describe"),
        ("made-up-name", "describe"),
        ("unknown-reads-good", "read.unknown"),
        ("act-accepts-anything", "act.unknown"),
        ("keeps-updating", "subscribe"),
    ],
)
async def test_the_suite_notices(flaw: str, check: str) -> None:
    pump = FakePump(frozenset({flaw}))
    try:
        findings = await in_process(pump)
    finally:
        for task in pump.leaked:
            task.cancel()
    assert [f.check for f in findings] == [check], findings


async def test_the_command_checks_a_plugin_in_another_process(short_dir: Path) -> None:
    path = short_dir / "plugins" / "s"
    suite = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "thermaestro.cap.conformance",
        "--unix",
        str(path),
        "--timeout",
        "3",
        "--wait",
        "30",
        stdout=asyncio.subprocess.PIPE,
    )
    try:
        for _ in range(100):
            if path.exists():
                break
            await asyncio.sleep(0.1)
        async with plugin_process("--unix", str(path)):
            out, _ = await asyncio.wait_for(suite.communicate(), 60)
    finally:
        if suite.returncode is None:
            suite.terminate()
            await suite.wait()
    assert suite.returncode == 0, out.decode()
    assert out.decode().splitlines()[-1] == "conforms"
