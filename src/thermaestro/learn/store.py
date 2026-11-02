"""Learned values in the database, with a copy in memory so decisions read them without
waiting on the disk. Starting a value over and pinning one are the household's, and
audited; learning itself isn't, as it changes values all the time."""

import json

from .. import clock
from ..core.audit import AuditLog
from ..store import Database, Transaction
from .model import Learned, Number, key, mark
from .validate import UNJUDGED

KEPT_PROOF = mark("Proved when last judged, %(seen)s; too little since to judge it again.")
SET_ASIDE = mark(
    "An earlier version kept this proof for values it may not have tested: it is judged again."
)
"""Why a proof kept by an earlier version is no longer: it used values added since it was
proved (database migration 31)."""


class NotLearned(KeyError):
    pass


def _proof_stands(now: Learned, before: Learned) -> bool:
    """A model proved before (and kept while frozen), now only not judged for too little to
    judge it by, and learned from the same inputs."""
    return (
        before.state in ("proved", "frozen")
        and now.kind == "model"
        and now.state == "learning"
        and now.why in UNJUDGED
        and now.fingerprint == before.fingerprint
    )


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
        for found in list(self._kept.values()):
            if found.why == KEPT_PROOF and found.proved is None:
                # Kept by an earlier version, which used values added since (migration 31
                # does this on upgrade; a row written so afterward is set aside here).
                await self._write(
                    found.model_copy(update={"state": "learning", "why": SET_ASIDE, "args": {}})
                )

    def all(self) -> list[Learned]:
        return sorted(self._kept.values(), key=lambda x: (x.scope, x.name))

    def get(self, name: str, scope: str) -> Learned | None:
        return self._kept.get(key(name, scope))

    def used(self, name: str, scope: str) -> Number | None:
        found = self.get(name, scope)
        return None if found is None else found.used

    async def put(self, learned: Learned) -> Learned:
        """Keep what a learner found. A pin stays: only the household sets or lifts it. A
        proof stands until a test with enough to judge by fails (`validate`)."""
        before = self._kept.get(learned.key)
        pinned = before.pinned if before is not None else None
        kept = learned.model_copy(update={"pinned": pinned, "updated": clock.time()})
        if before is not None and _proof_stands(kept, before):
            # What was proved stays in use; the newest value goes on learning beside it (a
            # model learned day by day, the tank's week, learns from it), and is used once
            # a test with enough to judge by proves it.
            seen = before.args.get("seen") if before.why == KEPT_PROOF else before.updated
            kept = kept.model_copy(
                update={
                    "value": kept.value if kept.value is not None else before.value,
                    "proved": before.proved if before.proved is not None else before.value,
                    "state": "proved",
                    "why": KEPT_PROOF,
                    "args": {"seen": seen if seen is not None else clock.time()},
                    "n": max(kept.n, before.n),
                    "since": before.since or kept.since,
                }
            )
        elif before is not None and kept.state == "frozen":
            # Frozen isn't judged: what was in use stays in use.
            kept = kept.model_copy(update={"proved": before.proved})
        else:
            kept = kept.model_copy(update={"proved": None})  # judged now, or not proved
        await self._write(kept)
        return kept

    async def start_over(self, name_key: str, who: str) -> Learned:
        """Forget what was learned; the starting value stands until it is learned again. A
        pin stays."""
        before = self._find(name_key)
        kept = before.model_copy(
            update={
                "value": None,
                "proved": None,
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
