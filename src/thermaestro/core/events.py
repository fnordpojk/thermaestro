"""Events: what happened that a person may want to know, raised once and cleared once.

An event is about a condition, named by its key (`floor.broken:pump:hp1/dhw`): raised
while the condition holds, cleared when it ends. Raised again while open, it changes
nothing, unless what it says changed; raised after it cleared, it is a new event. A moment,
such as a setting put back, is raised and cleared at once.

What an event says is kept as its text, marked for translation, and the values put in it,
so each page shows it in its reader's language. Who it concerns is kept too: `technical`
events are for those who may see the house's workings, `household` ones also for the
person whose request it is about (`principal`). Notifications are sent from here, to the
people who turned them on.
"""

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

from .. import clock
from ..store import Database, Transaction

Audience = Literal["technical", "household"]


def mark(text: str) -> str:
    """Marks a message for the web UI's catalog: shown translated, kept in English here."""
    return text


PUT_BACK = mark(
    "The planner stopped answering: every setting Thermaestro changed was put back as found"
)
KEEP_S = 90 * 86_400.0
"""How long an event is kept after it cleared."""


@dataclass(frozen=True)
class Event:
    id: int
    key: str
    kind: str
    """What it is about, without its scope: `floor.broken`, `intent.at_risk`."""
    scope: str
    audience: Audience
    message: str
    """The text, as marked for translation, with `%(name)s` for each value."""
    args: dict[str, Any] = field(default_factory=dict)
    raised: float = 0.0
    changed: float = 0.0
    """When what it says last changed."""
    cleared: float | None = None
    principal: str | None = None
    """Whose request it is about, for a household event."""

    @property
    def open(self) -> bool:
        return self.cleared is None

    def text(self) -> str:
        """What it says, in English: for logs and the API."""
        return self.message % self.args if self.args else self.message


@dataclass(frozen=True)
class Raise:
    """An event wanted: what `Events.sync` raises for a key."""

    kind: str
    scope: str
    message: str
    args: Mapping[str, Any] = field(default_factory=dict)
    audience: Audience = "technical"
    principal: str | None = None


COLUMNS = "id, key, kind, scope, audience, principal, message, args, raised, changed, cleared"


def _event(row: tuple[Any, ...]) -> Event:
    id, key, kind, scope, audience, principal, message, args, raised, changed, cleared = row
    return Event(
        id,
        key,
        kind,
        scope,
        audience,
        message,
        json.loads(args),
        raised,
        changed,
        cleared,
        principal,
    )


class Events:
    def __init__(self, db: Database) -> None:
        self._db = db
        self.listeners: list[Any] = []
        """Called with each event raised, changed or cleared: for notifications."""

    async def raise_(
        self,
        key: str,
        what: Raise,
        *,
        now: float | None = None,
    ) -> Event | None:
        """Raise the event for `key`; None if it was open and says the same."""
        t = clock.time() if now is None else now
        args = json.dumps(dict(what.args), sort_keys=True, default=str)

        def write(tx: Transaction) -> Event | None:
            row = tx.execute(
                f"SELECT {COLUMNS} FROM events WHERE key = ? AND cleared IS NULL",  # noqa: S608
                (key,),
            ).fetchone()
            if row is not None:
                was = _event(row)
                if was.message == what.message and json.dumps(was.args, sort_keys=True) == args:
                    return None
                tx.execute(
                    "UPDATE events SET message = ?, args = ?, changed = ? WHERE id = ?",
                    (what.message, args, t, was.id),
                )
                return _get(tx, was.id)
            cursor = tx.execute(
                "INSERT INTO events (key, kind, scope, audience, principal, message, args,"
                " raised, changed) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    key,
                    what.kind,
                    what.scope,
                    what.audience,
                    what.principal,
                    what.message,
                    args,
                    t,
                    t,
                ),
            )
            tx.execute(
                "DELETE FROM events WHERE cleared IS NOT NULL AND cleared < ?", (t - KEEP_S,)
            )
            return _get(tx, int(cursor.lastrowid or 0))

        found = await self._db.run(write)
        if found is not None:
            self._tell(found)
        return found

    async def clear(self, key: str, *, now: float | None = None) -> Event | None:
        """Clear the open event for `key`; None if none was open."""
        t = clock.time() if now is None else now

        def write(tx: Transaction) -> Event | None:
            row = tx.execute(
                "SELECT id FROM events WHERE key = ? AND cleared IS NULL", (key,)
            ).fetchone()
            if row is None:
                return None
            tx.execute("UPDATE events SET cleared = ? WHERE id = ?", (t, row[0]))
            return _get(tx, row[0])

        found = await self._db.run(write)
        if found is not None:
            self._tell(found)
        return found

    async def moment(self, key: str, what: Raise, *, now: float | None = None) -> Event | None:
        """An event that is over as it happens: raised and cleared at once."""
        t = clock.time() if now is None else now
        raised = await self.raise_(key, what, now=t)
        await self.clear(key, now=t)
        return raised

    async def sync(
        self, prefix: str, wanted: Mapping[str, Raise], *, now: float | None = None
    ) -> None:
        """Those under `prefix` as `wanted` says: each wanted one raised (or kept), every
        other open one under it cleared."""
        for key, what in wanted.items():
            await self.raise_(key, what, now=now)
        for event in await self.open(prefix):
            if event.key not in wanted:
                await self.clear(event.key, now=now)

    async def open(self, prefix: str = "") -> list[Event]:
        rows = await self._db.run(
            lambda tx: tx.execute(
                f"SELECT {COLUMNS} FROM events WHERE cleared IS NULL"  # noqa: S608
                " AND key LIKE ? ESCAPE '!' ORDER BY raised",
                (_like(prefix),),
            ).fetchall()
        )
        return [_event(r) for r in rows]

    async def since(self, t: float, prefix: str = "") -> list[Event]:
        """Every event open at `t` or raised after, oldest first."""
        rows = await self._db.run(
            lambda tx: tx.execute(
                f"SELECT {COLUMNS} FROM events WHERE (cleared IS NULL OR cleared >= ?)"  # noqa: S608
                " AND key LIKE ? ESCAPE '!' ORDER BY raised",
                (t, _like(prefix)),
            ).fetchall()
        )
        return [_event(r) for r in rows]

    def _tell(self, event: Event) -> None:
        for listener in list(self.listeners):
            listener(event)


def _get(tx: Transaction, id: int) -> Event:
    row = tx.execute(f"SELECT {COLUMNS} FROM events WHERE id = ?", (id,)).fetchone()  # noqa: S608
    return _event(row)


def _like(prefix: str) -> str:
    """A pattern for keys that start with `prefix`, its own `%` and `_` taken as they are."""
    escaped = prefix.replace("!", "!!").replace("%", "!%").replace("_", "!_")
    return escaped + "%"
