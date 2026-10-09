import asyncio
import hashlib
import json
import os
import signal
import socket
import sqlite3
import ssl
import sys
from contextlib import closing
from pathlib import Path

import httpx
from capfake import FakePump
from leverfake import LeverDevice

from thermaestro.auth import SetupCode
from thermaestro.core import Core, Key, run
from thermaestro.store import Control, Database, Layout, Plugin
from thermaestro.web import certificate


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port: int = s.getsockname()[1]
        return port


def layout(tmp_path: Path) -> Layout:
    """A layout whose web UI listens on free local ports."""
    lay = Layout(tmp_path / "config", tmp_path / "state")
    lay.config.mkdir()
    lay.startup.write_text(
        f'[web]\nlisten = "127.0.0.1"\nport = {free_port()}\nhttps_port = {free_port()}\n'
    )
    return lay


def ports(lay: Layout) -> tuple[int, int]:
    text = lay.startup.read_text()
    found = dict(line.split(" = ") for line in text.splitlines() if "port" in line)
    return int(found["port"]), int(found["https_port"])


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


async def test_stopping_puts_every_lever_back(tmp_path: Path) -> None:
    lay = layout(tmp_path)
    lay.state.mkdir(mode=0o700)
    async with await Database.open(lay.database) as db:
        await db.put(Plugin(plugin="leverfake"), "dev")
        await db.put(Control(levers={"dev:hp1/offset": "control", "dev:hp1/block": "control"}))
    device = LeverDevice()
    stop = asyncio.Event()
    outcomes: list[str] = []

    async def started(core: Core) -> None:
        async with asyncio.timeout(10):
            while Key("dev", "hp1/x.fake.offset") not in core.values.latest:
                await asyncio.sleep(0.05)
        for ref, op, params in (
            ("dev:hp1/offset", "set", {"value": 3}),
            ("dev:hp1/block", "engage", {}),
        ):
            result = await core.executor.act(ref, op, params, who="test")  # type: ignore[arg-type]
            outcomes.append(result.outcome)
        stop.set()

    await asyncio.wait_for(
        run(lay, stop=stop, factories={"leverfake": lambda c: device}, started=started), 20
    )
    assert outcomes == ["verified", "awaiting_effect"]
    assert device.registers["x.fake.offset"] == -4
    assert device.held == set()
    whats = audit_whats(lay.state)
    assert whats.index("lever.restore") < whats.index("core.stop")


async def test_the_web_ui_runs_inside(tmp_path: Path) -> None:
    lay = layout(tmp_path)
    http_port, https_port = ports(lay)
    stop = asyncio.Event()
    task = asyncio.create_task(run(lay, stop=stop, factories={}))
    try:
        async with asyncio.timeout(10):
            while "core.start" not in audit_whats(lay.state):
                await asyncio.sleep(0.05)
            while True:
                try:
                    async with httpx.AsyncClient() as client:
                        answer = await client.get(f"http://127.0.0.1:{http_port}/health")
                    break
                except httpx.ConnectError:
                    await asyncio.sleep(0.05)
        assert answer.json() == {"status": "ok"}
        # No administrator yet: a setup code, readable by the service user only.
        assert SetupCode(lay.setup_code).current() is not None
        assert (lay.setup_code.stat().st_mode & 0o777) == 0o600
        # HTTPS with the installation's own certificate.
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        async with httpx.AsyncClient(verify=context) as client:
            secure = await client.get(f"https://127.0.0.1:{https_port}/login")
        assert secure.status_code == 200
        assert (lay.state / "tls" / "key.pem").stat().st_mode & 0o777 == 0o600
        der = ssl.PEM_cert_to_DER_cert((lay.state / "tls" / "cert.pem").read_text())
        _, writer = await asyncio.open_connection("127.0.0.1", https_port, ssl=context)
        presented = writer.get_extra_info("ssl_object").getpeercert(binary_form=True)
        writer.close()
        await writer.wait_closed()
        assert presented == der
        # The fingerprint the UI shows is the one a browser computes.
        expected = ":".join(f"{b:02X}" for b in hashlib.sha256(der).digest())
        assert certificate.ensure(lay.tls).fingerprint == expected
    finally:
        stop.set()
        await asyncio.wait_for(task, 10)


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


async def test_the_status_command_reads_through_the_api(tmp_path: Path) -> None:
    lay = layout(tmp_path)
    http_port, _ = ports(lay)
    lay.state.mkdir(mode=0o700)
    async with await Database.open(lay.database) as db:
        await db.put(Plugin(plugin="fake"), "pump")
    stop = asyncio.Event()
    task = asyncio.create_task(run(lay, stop=stop, factories={"fake": lambda c: FakePump()}))
    try:
        async with asyncio.timeout(10):
            while "plugin.start" not in audit_whats(lay.state):
                await asyncio.sleep(0.05)
        from thermaestro.auth import Accounts, Principal
        from thermaestro.core import AuditLog

        async with await Database.open(lay.database) as db:
            accounts = Accounts(db, AuditLog(lay.audit))
            await accounts.set_group("Readers", ["points.read", "tokens.own"], by="cli")
            anna = await accounts.create_user(
                "anna", "correct horse battery staple", ["Readers"], by="cli"
            )
            token = await accounts.create_token(Principal(anna), "cli", ["points.read"])
        url = f"http://127.0.0.1:{http_port}"
        command = [sys.executable, "-m", "thermaestro.cli", "status", "--url", url]
        process = await asyncio.create_subprocess_exec(
            *command,
            env=dict(os.environ, THERMAESTRO_TOKEN=token),
            stdout=asyncio.subprocess.PIPE,
        )
        out, _ = await process.communicate()
        assert process.returncode == 0
        assert "pump (fake): up" in out.decode()
        assert "hp1/outdoor.temp" in out.decode()
        refused = await asyncio.create_subprocess_exec(
            *command,
            env=dict(os.environ, THERMAESTRO_TOKEN="thm_wrong"),
            stderr=asyncio.subprocess.PIPE,
        )
        _, err = await refused.communicate()
        assert refused.returncode == 1
        assert "401" in err.decode()
    finally:
        stop.set()
        await asyncio.wait_for(task, 10)
