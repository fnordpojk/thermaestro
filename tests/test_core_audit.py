import json
import stat
from pathlib import Path

from thermaestro.cli import main
from thermaestro.core import AuditLog, verify


async def test_entries_chain_and_verify(tmp_path: Path) -> None:
    log = AuditLog(tmp_path / "audit")
    await log.record("user:anna", "login", source="192.0.2.5")
    await log.record("core", "core.start")
    path = tmp_path / "audit" / "audit.jsonl"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE((tmp_path / "audit").stat().st_mode) == 0o700
    entries = [json.loads(line) for line in path.read_text().splitlines()]
    assert [e["what"] for e in entries] == ["login", "core.start"]
    assert entries[0]["prev"] == "0" * 64
    assert entries[0]["from"] == "192.0.2.5"
    assert verify(path) == []


async def test_the_chain_continues_after_a_restart(tmp_path: Path) -> None:
    await AuditLog(tmp_path).record("core", "core.start")
    await AuditLog(tmp_path).record("core", "core.start")
    assert verify(tmp_path / "audit.jsonl") == []


async def test_an_edited_line_breaks_the_chain(tmp_path: Path) -> None:
    log = AuditLog(tmp_path)
    for what in ("a", "b", "c"):
        await log.record("core", what)
    path = tmp_path / "audit.jsonl"
    lines = path.read_text().splitlines()
    lines[1] = lines[1].replace('"b"', '"x"')
    path.write_text("\n".join(lines) + "\n")
    assert verify(path) == ["line 3: the chain is broken before this line"]


async def test_a_removed_line_breaks_the_chain(tmp_path: Path) -> None:
    log = AuditLog(tmp_path)
    for what in ("a", "b", "c"):
        await log.record("core", what)
    path = tmp_path / "audit.jsonl"
    lines = path.read_text().splitlines()
    path.write_text("\n".join([lines[0], lines[2]]) + "\n")
    assert verify(path) == ["line 2: the chain is broken before this line"]


async def test_details_cant_forge_a_line(tmp_path: Path) -> None:
    log = AuditLog(tmp_path)
    await log.record("user:x", "setting.change", details={"name": 'a"\n{"what":"forged"}'})
    assert len((tmp_path / "audit.jsonl").read_text().splitlines()) == 1


async def test_the_command(tmp_path: Path) -> None:
    log = AuditLog(tmp_path)
    await log.record("core", "a")
    await log.record("core", "b")
    path = tmp_path / "audit.jsonl"
    assert main(["audit", "verify", str(path)]) == 0
    path.write_text(path.read_text().replace('"a"', '"z"'))
    assert main(["audit", "verify", str(path)]) == 1


async def test_two_writers_keep_one_chain(tmp_path: Path) -> None:
    # The service and the command line append to the same file.
    service, cli = AuditLog(tmp_path), AuditLog(tmp_path)
    for log in (service, cli, service, service, cli, service):
        await log.record("core", "x")
    assert verify(tmp_path / "audit.jsonl") == []
