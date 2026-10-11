"""Learned values in the database, with a copy in memory so decisions read them without
waiting on the disk. Starting a value over and pinning one are the household's, and
audited; learning itself isn't, as it changes values all the time."""

import json

from .. import clock
from ..core.audit import AuditLog
from ..store import Database, Transaction
from .model import Learned, Number, key, mark


class NotLearned(KeyError):
    pass


class Learnings:
    def __init__(self, db: Database, audit: AuditLog) -> None:
        self._db = db
        self._audit = audit
        self._kept: dict[str, Learned] = {}

    async def load(self) -> None:
        def read(t: Transaction) -> dict[str, Learned]:
            rows = t.execute("SELECT key, body FROM learned")
            return {k: Learned.model_validate_json(body) for k, body in rows}

        self._kept = await self._db.run(read)

    def all(self) -> list[Learned]:
        return sorted(self._kept.values(), key=lambda x: (x.scope, x.name))

    def get(self, name: str, scope: str) -> Learned | None:
        return self._kept.get(key(name, scope))

    def used(self, name: str, scope: str) -> Number | None:
        found = self.get(name, scope)
        return None if found is None else found.used

    async def put(self, learned: Learned) -> Learned:
        """Keep what a learner found. A pin stays: only the household sets or lifts it."""
        before = self._kept.get(learned.key)
        pinned = before.pinned if before is not None else None
        kept = learned.model_copy(update={"pinned": pinned, "updated": clock.time()})
        await self._write(kept)
        return kept

    async def start_over(self, name_key: str, who: str) -> Learned:
        """Forget what was learned; the starting value stands until it is learned again. A
        pin stays."""
        before = self._find(name_key)
        kept = before.model_copy(
            update={
                "value": None,
                "state": "starting",
                "why": mark("Started over."),
                "args": {},
                "n": 0,
                "since": None,
                "updated": clock.time(),
            }
        )
        await self._write(kept)
        await self._audit.record(
            who, "learned.start_over", details={"learned": name_key, "was": _shown(before.value)}
        )
        return kept

    async def pin(self, name_key: str, value: float | None, who: str) -> Learned:
        """Use `value` instead of what was learned; None lifts the pin."""
        before = self._find(name_key)
        if value is not None and isinstance(before.value, dict):
            raise ValueError("this can't be pinned: it isn't one number")
        kept = before.model_copy(update={"pinned": value, "updated": clock.time()})
        await self._write(kept)
        await self._audit.record(
            who,
            "learned.pin",
            details={"learned": name_key, "pinned": value, "was": before.pinned},
        )
        return kept

    def _find(self, name_key: str) -> Learned:
        found = self._kept.get(name_key)
        if found is None:
            raise NotLearned(name_key)
        return found

    async def _write(self, learned: Learned) -> None:
        body = learned.model_dump_json()

        def write(t: Transaction) -> None:
            t.execute(
                "INSERT INTO learned (key, body, updated) VALUES (?, ?, ?)"
                " ON CONFLICT (key) DO UPDATE SET body = excluded.body, updated = excluded.updated",
                (learned.key, body, learned.updated or clock.time()),
            )

        await self._db.run(write)
        self._kept[learned.key] = learned


def _shown(value: Number | None) -> str | None:
    return None if value is None else json.dumps(value)
