"""Intents kept: asked for, checked, stored, ended, confirmed, and moved along with time.

Rights decide who may ask for what (the kind, and for the cost stance, which part of it);
they don't decide what wins. Every change is in the audit log.
"""

from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Any, Protocol

from .. import clock
from ..auth.permissions import allows
from ..store import Database, Home, Location, Transaction
from .calendar import Calendar
from .entry import Capabilities, Verdict, check
from .model import OPEN, Intent, Level, State
from .resolve import InForce, Resolver
from .seed import SEED, Found, seed


class Audit(Protocol):
    async def record(
        self,
        who: str,
        what: str,
        *,
        outcome: str = "ok",
        why: str | None = None,
        source: str | None = None,
        details: Mapping[str, Any] | None = None,
    ) -> None: ...


class Forbidden(Exception):
    def __init__(self, right: str) -> None:
        super().__init__(f"needs the right {right}")
        self.right = right


class NotFound(Exception):
    pass


TEMPORARY_RIGHTS = {
    "warmer": "intent.temporary.create",
    "bath": "intent.temporary.create",
    "boost_now": "intent.temporary.create",
    "fireplace": "intent.temporary.create",
    "away": "intent.temporary.create.away",
    "guests": "intent.temporary.create.guests",
    "hands_off": "intent.handsoff.create",
}


def rights_for(intent: Intent, previous: Intent | None = None) -> set[str]:
    """What it takes to ask for an intent, or to change `previous` into it."""
    if intent.kind in TEMPORARY_RIGHTS:
        return {TEMPORARY_RIGHTS[intent.kind]}
    if intent.kind == "cost_stance":
        needed = set()
        before = previous.parameters if previous is not None else {}
        if before.get("ranking") != intent.parameters.get("ranking"):
            needed.add("intent.ranking.write")
        if before.get("slider") != intent.parameters.get("slider"):
            needed.add("intent.slider.write")
        return needed or {"intent.slider.write"}
    return {"intent.standing.write"}


class Intents:
    def __init__(
        self,
        db: Database,
        audit: Audit,
        *,
        capabilities: Callable[[], Capabilities] = Capabilities,
    ) -> None:
        self._db = db
        self._audit = audit
        self._capabilities = capabilities

    # --- reading ---------------------------------------------------------------------------

    async def all(self, *, open_only: bool = True) -> list[Intent]:
        def read(t: Transaction) -> list[Intent]:
            sql = "SELECT body FROM intents"
            if open_only:
                sql += " WHERE ended IS NULL"
            return [
                Intent.model_validate_json(body) for (body,) in t.execute(sql + " ORDER BY created")
            ]

        return await self._db.run(read)

    async def get(self, id: str) -> Intent:
        def read(t: Transaction) -> Intent | None:
            row = t.execute("SELECT body FROM intents WHERE id = ?", (id,)).fetchone()
            return None if row is None else Intent.model_validate_json(row[0])

        found = await self._db.run(read)
        if found is None:
            raise NotFound(id)
        return found

    async def levels(self) -> dict[str, Level]:
        def read(t: Transaction) -> dict[str, Level]:
            rows = t.execute("SELECT body FROM levels ORDER BY id")
            return {level.id: level for level in (Level.model_validate_json(b) for (b,) in rows)}

        return await self._db.run(read)

    async def calendar(self) -> Calendar:
        location = await self._db.get(Location)
        home = await self._db.get(Home) or Home()
        zone = location.timezone if location is not None else "UTC"
        return Calendar(zone, home.holidays, home.holidays_as)

    async def resolver(self) -> Resolver:
        return Resolver(
            await self.all(),
            await self.levels(),
            await self.calendar(),
            dict(self._capabilities().rooms),
        )

    async def in_force(
        self, at: datetime | None = None, *, heating: bool = True, outdoor: float | None = None
    ) -> InForce:
        return (await self.resolver()).in_force(at or clock.now(), heating=heating, outdoor=outdoor)

    # --- asking ----------------------------------------------------------------------------

    async def create(
        self, intent: Intent, *, granted: frozenset[str], now: datetime | None = None
    ) -> Verdict:
        """Check an intent and keep it if it passes. `granted`: the asker's rights; the
        asker is the intent's principal."""
        for right in rights_for(intent):
            _require(granted, right)
        now = now or clock.now()
        others = await self.all()
        verdict = check(
            intent, others, await self.levels(), self._capabilities(), await self.calendar(), now
        )
        if verdict.accepted:
            await self._db.run(lambda t: _store(t, verdict.intent, now))
        await self._audit.record(
            intent.principal,
            "intent.create",
            outcome="ok" if verdict.accepted else "failed",
            why=None if verdict.accepted else verdict.intent.why,
            details={
                "id": intent.id,
                "kind": intent.kind,
                "scope": intent.scope,
                "messages": list(verdict.messages),
            },
        )
        return verdict

    async def end(
        self, id: str, *, who: str, granted: frozenset[str], why: str | None = None
    ) -> Intent:
        """End an intent early: one's own with the right to ask for it, anyone's with
        `intent.any.end`."""
        intent = await self.get(id)
        if not allows(granted, "intent.any.end"):
            if intent.principal != who:
                raise Forbidden("intent.any.end")
            for right in rights_for(intent):
                _require(granted, right)
        return await self._change(intent, "finished", why or f"ended by {who}", who)

    async def confirm(self, id: str, *, who: str, granted: frozenset[str]) -> Intent:
        """A seeded or learned intent becomes the household's own."""
        _require(granted, "intent.standing.write")
        intent = await self.get(id)
        tier = "standing" if intent.tier == "default" else intent.tier
        confirmed = intent.model_copy(update={"confirmed": True, "tier": tier})
        now = clock.now()
        await self._db.run(lambda t: _store(t, confirmed, now))
        await self._audit.record(who, "intent.confirm", details={"id": id, "kind": intent.kind})
        return confirmed

    async def report(self, id: str, state: State, why: str | None = None) -> Intent:
        """What the planner finds: at risk, giving way, met, missed."""
        return await self._change(await self.get(id), state, why, "planner")

    async def put_level(self, level: Level, *, who: str, granted: frozenset[str]) -> None:
        _require(granted, "intent.levels.write")
        await self._db.run(
            lambda t: t.execute(
                "INSERT INTO levels (id, body) VALUES (?, ?)"
                " ON CONFLICT (id) DO UPDATE SET body = excluded.body",
                (level.id, level.model_dump_json()),
            )
        )
        await self._audit.record(who, "intent.level", details=level.model_dump(mode="json"))

    async def delete_level(self, id: str, *, who: str, granted: frozenset[str]) -> None:
        _require(granted, "intent.levels.write")
        using = [i.id for i in await self.all() if _names(i, id)]
        if using:
            raise ValueError(f"level {id!r} is used by {', '.join(using)}")
        await self._db.run(lambda t: t.execute("DELETE FROM levels WHERE id = ?", (id,)))
        await self._audit.record(who, "intent.level.delete", details={"id": id})

    async def seed(self, found: Found, now: datetime | None = None) -> list[Intent]:
        """Seed defaults from how the house runs now, where nothing of the kind is there;
        they apply until confirmed or replaced."""
        now = now or clock.now()
        known = await self.levels()
        new_levels, drafts = seed(found, await self.all(), now)
        new_levels = [level for level in new_levels if level.id not in known]
        active = [d.model_copy(update={"state": "active"}) for d in drafts]

        def write(t: Transaction) -> None:
            for level in new_levels:
                t.execute(
                    "INSERT OR IGNORE INTO levels (id, body) VALUES (?, ?)",
                    (level.id, level.model_dump_json()),
                )
            for intent in active:
                _store(t, intent, now)

        await self._db.run(write)
        if active or new_levels:
            await self._audit.record(
                SEED,
                "intent.seed",
                details={
                    "levels": [level.model_dump(mode="json") for level in new_levels],
                    "intents": [{"id": i.id, "kind": i.kind, "scope": i.scope} for i in active],
                },
            )
        return active

    # --- with time -------------------------------------------------------------------------

    async def advance(self, now: datetime | None = None) -> list[Intent]:
        """Move intents along: scheduled ones start, ended ones finish. A deadline that
        ends unmet is missed. Returns the ones that changed."""
        now = now or clock.now()
        resolver = await self.resolver()
        changed = []
        for intent in resolver.intents:
            end = resolver.end(intent)
            start = intent.validity.starts or intent.created
            if end is not None and end <= now:
                if intent.validity.ends == "when_met":
                    state, why = "missed", "not met by its latest"
                else:
                    state, why = "finished", None
                changed.append(await self._change(intent, state, why, "core", now))  # type: ignore[arg-type]
            elif intent.state == "scheduled" and start <= now:
                changed.append(await self._change(intent, "active", None, "core", now))
        return changed

    async def _change(
        self, intent: Intent, state: State, why: str | None, who: str, now: datetime | None = None
    ) -> Intent:
        now = now or clock.now()
        changed = intent.model_copy(update={"state": state, "why": why})
        await self._db.run(lambda t: _store(t, changed, now))
        await self._audit.record(
            who,
            "intent.state",
            why=why,
            details={"id": intent.id, "kind": intent.kind, "from": intent.state, "to": state},
        )
        return changed


def _require(granted: frozenset[str], right: str) -> None:
    if not allows(granted, right):
        raise Forbidden(right)


def _names(intent: Intent, level: str) -> bool:
    if any(t.level == level for e in intent.expectations for t in e.targets):
        return True
    chosen = intent.parameters.get("levels")
    in_chosen = isinstance(chosen, dict) and level in chosen.values()
    return in_chosen or intent.parameters.get("hot_water") == level


def _store(t: Transaction, intent: Intent, now: datetime) -> None:
    ended = None if intent.state in OPEN else now.timestamp()
    t.execute(
        "INSERT INTO intents (id, body, state, created, ended) VALUES (?, ?, ?, ?, ?)"
        " ON CONFLICT (id) DO UPDATE SET body = excluded.body, state = excluded.state,"
        " ended = excluded.ended",
        (intent.id, intent.model_dump_json(), intent.state, intent.created.timestamp(), ended),
    )
