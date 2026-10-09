"""The audit log: who did what, when, from where, why, and how it ended.

Append-only JSON lines, separate from the program's log. Each line carries the hash of
the line before it, so an edited, removed or reordered line breaks the chain, and
`verify` says where. Lines cut off the end leave no break; forwarding the log off the
host is what catches that. Values of secrets never go in; their names may.
"""

import asyncio
import fcntl
import hashlib
import json
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .. import clock
from ..files import private_directory

GENESIS = "0" * 64
FILE = "audit.jsonl"


def _hash(line: bytes) -> str:
    return hashlib.sha256(line).hexdigest()


class AuditLog:
    def __init__(self, directory: Path) -> None:
        self.path = directory / FILE
        self._lock = asyncio.Lock()
        self._previous: str | None = None
        self._size = -1

    async def record(
        self,
        who: str,
        what: str,
        *,
        outcome: str = "ok",
        why: str | None = None,
        source: str | None = None,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        """Append one entry; it is on disk when this returns. `who` is a user, a token or a
        principal (`planner`, `mqtt`, `plugin:<name>`, `cli`, `core`); `source` the client
        address where there is one."""
        async with self._lock:
            await asyncio.to_thread(self._append, who, what, outcome, why, source, details)

    def _append(
        self,
        who: str,
        what: str,
        outcome: str,
        why: str | None,
        source: str | None,
        details: Mapping[str, Any] | None,
    ) -> None:
        if self._previous is None:
            private_directory(self.path.parent)
        # The command line appends too (`thermaestro admin ...`), possibly while the
        # service runs: a lock, and the last hash read again if the file grew under us.
        fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            if self._previous is None or os.fstat(fd).st_size != self._size:
                self._previous = _last_hash(self.path)
            line = self._line(who, what, outcome, why, source, details)
            os.write(fd, line + b"\n")
            os.fsync(fd)
            self._size = os.fstat(fd).st_size
        finally:
            os.close(fd)  # also releases the lock
        self._previous = _hash(line)

    def _line(
        self,
        who: str,
        what: str,
        outcome: str,
        why: str | None,
        source: str | None,
        details: Mapping[str, Any] | None,
    ) -> bytes:
        entry = {
            "t": clock.now().isoformat(timespec="milliseconds"),
            "who": who,
            "from": source,
            "what": what,
            "why": why,
            "outcome": outcome,
            "details": dict(details or {}),
            "prev": self._previous,
        }
        # json.dumps escapes CR and LF, so an entry can never forge a second line.
        return json.dumps(entry, ensure_ascii=False, separators=(",", ":")).encode()


def _last_hash(path: Path) -> str:
    try:
        lines = path.read_bytes().splitlines()
    except FileNotFoundError:
        return GENESIS
    return _hash(lines[-1]) if lines else GENESIS


def verify(path: Path) -> list[str]:
    """The problems found in the chain; an empty list means it is intact."""
    problems = []
    previous = GENESIS
    try:
        lines = path.read_bytes().splitlines()
    except FileNotFoundError:
        return []
    for number, line in enumerate(lines, start=1):
        try:
            prev = json.loads(line)["prev"]
        except (ValueError, KeyError, TypeError):
            problems.append(f"line {number}: not an audit entry")
        else:
            if prev != previous:
                problems.append(f"line {number}: the chain is broken before this line")
        previous = _hash(line)
    return problems
