"""The state database: everything set in the UI or learned.

SQLite, with every change in a transaction, so a crash leaves either the old state or
the new, never half of one. Every call runs on one worker thread that owns the
connection, so the event loop never waits on the disk.

The schema is versioned from the first version: each migration runs once, in its own
transaction, and the database records the version it reached.
"""

import asyncio
import contextlib
import logging
import os
import sqlite3
from collections.abc import Callable, Iterable, Sequence
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Self

from pydantic import ValidationError

from .. import clock
from ..files import UnsafePath, check_private_file
from .errors import StoreError, name_fields
from .settings import Setting

log = logging.getLogger(__name__)

MIGRATIONS: tuple[str, ...] = (
    # 1: settings, one JSON document per kind and id
    """
    CREATE TABLE settings (
        kind TEXT NOT NULL,
        id TEXT NOT NULL,
        body TEXT NOT NULL CHECK (json_valid(body)),
        updated TEXT NOT NULL,
        PRIMARY KEY (kind, id)
    ) STRICT;
    """,
    # 2: value history: samples as they came, and 15-minute aggregates kept longer
    """
    CREATE TABLE history (
        instance TEXT NOT NULL,
        point TEXT NOT NULL,
        t REAL NOT NULL,
        value REAL,
        text TEXT,
        quality TEXT NOT NULL,
        PRIMARY KEY (instance, point, t)
    ) STRICT, WITHOUT ROWID;
    CREATE TABLE history_15m (
        instance TEXT NOT NULL,
        point TEXT NOT NULL,
        slot REAL NOT NULL,
        min REAL NOT NULL,
        mean REAL NOT NULL,
        max REAL NOT NULL,
        last REAL NOT NULL,
        n INTEGER NOT NULL,
        PRIMARY KEY (instance, point, slot)
    ) STRICT, WITHOUT ROWID;
    """,
    # 3: accounts: users, groups and their rights, sessions, API tokens
    """
    CREATE TABLE users (
        id INTEGER PRIMARY KEY,
        name TEXT NOT NULL UNIQUE COLLATE NOCASE,
        password TEXT NOT NULL,
        created REAL NOT NULL,
        failures INTEGER NOT NULL DEFAULT 0,
        locked_until REAL NOT NULL DEFAULT 0,
        disabled INTEGER NOT NULL DEFAULT 0
    ) STRICT;
    CREATE TABLE groups (name TEXT PRIMARY KEY) STRICT;
    CREATE TABLE group_permissions (
        group_name TEXT NOT NULL REFERENCES groups (name) ON DELETE CASCADE,
        permission TEXT NOT NULL,
        PRIMARY KEY (group_name, permission)
    ) STRICT;
    CREATE TABLE user_groups (
        user_id INTEGER NOT NULL REFERENCES users (id) ON DELETE CASCADE,
        group_name TEXT NOT NULL REFERENCES groups (name) ON DELETE CASCADE,
        PRIMARY KEY (user_id, group_name)
    ) STRICT;
    CREATE TABLE user_permissions (
        user_id INTEGER NOT NULL REFERENCES users (id) ON DELETE CASCADE,
        permission TEXT NOT NULL,
        PRIMARY KEY (user_id, permission)
    ) STRICT;
    CREATE TABLE sessions (
        hash TEXT PRIMARY KEY,
        user_id INTEGER NOT NULL REFERENCES users (id) ON DELETE CASCADE,
        created REAL NOT NULL,
        last_seen REAL NOT NULL,
        expires REAL NOT NULL,
        confirmed REAL NOT NULL,
        source TEXT,
        agent TEXT
    ) STRICT;
    CREATE TABLE tokens (
        id INTEGER PRIMARY KEY,
        hash TEXT NOT NULL UNIQUE,
        user_id INTEGER NOT NULL REFERENCES users (id) ON DELETE CASCADE,
        name TEXT NOT NULL,
        permissions TEXT NOT NULL CHECK (json_valid(permissions)),
        created REAL NOT NULL,
        expires REAL NOT NULL,
        last_used REAL
    ) STRICT;
    INSERT INTO groups (name) VALUES ('Administrators'), ('Household'), ('Viewers');
    INSERT INTO group_permissions (group_name, permission) VALUES
        ('Administrators', '*'),
        ('Household', 'points.read'),
        ('Viewers', 'points.read');
    """,
    # 4: each user's language and formats
    """
    ALTER TABLE users ADD COLUMN preferences TEXT NOT NULL DEFAULT '{}'
        CHECK (json_valid(preferences));
    """,
    # 5: series: prices, rules and forecasts, interval by interval, the latest revision
    """
    CREATE TABLE intervals (
        instance TEXT NOT NULL,
        series TEXT NOT NULL,
        start REAL NOT NULL,
        end REAL NOT NULL,
        value REAL NOT NULL,
        unit TEXT NOT NULL,
        vat TEXT NOT NULL,
        status TEXT NOT NULL,
        revision INTEGER NOT NULL,
        published REAL,
        source TEXT,
        why TEXT,
        PRIMARY KEY (instance, series, start)
    ) STRICT, WITHOUT ROWID;
    """,
    # 6: forecasts kept at fixed lead times, and what was measured then, for scoring
    """
    CREATE TABLE forecast_leads (
        source TEXT NOT NULL,
        quantity TEXT NOT NULL,
        valid REAL NOT NULL,
        lead INTEGER NOT NULL,
        value REAL NOT NULL,
        made REAL NOT NULL,
        observed REAL,
        PRIMARY KEY (source, quantity, valid, lead)
    ) STRICT, WITHOUT ROWID;
    """,
    # 7: the broker's Home Assistant discovery switch became a setting of its own
    """
    UPDATE settings SET body = json_remove(body, '$.discovery') WHERE kind = 'mqtt';
    """,
    # 8: price layers lose the "remainder" a short-lived release stored; the price stack
    # now splits a supplier's total itself. A remainder layer was a supplier's total that
    # includes VAT, so it is that again, and VAT is no longer charged on it.
    """
    UPDATE settings SET body = json_set(body, '$.applies_to', (
        SELECT json_group_array(item.value)
        FROM json_each(settings.body, '$.applies_to') AS item
        WHERE item.value NOT IN (
            SELECT layer.id FROM settings AS layer
            WHERE layer.kind = 'price.layer'
            AND json_extract(layer.body, '$.source') = 'remainder'
        )
    ))
    WHERE kind = 'price.vat';
    UPDATE settings
    SET body = json_set(body, '$.source', 'series', '$.vat', 'incl')
    WHERE kind = 'price.layer' AND json_extract(body, '$.source') = 'remainder';
    UPDATE settings SET body = json_remove(body, '$.minus') WHERE kind = 'price.layer';
    """,
    # 9: the write path: each lever taken over, with what it was found at, and every
    # change made or, in shadow, decided. (IF NOT EXISTS: tests replay migrations on a
    # newer database by setting its version back.)
    """
    CREATE TABLE IF NOT EXISTS claims (
        lever TEXT PRIMARY KEY,
        claimed REAL NOT NULL,
        baseline TEXT CHECK (baseline IS NULL OR json_valid(baseline)),
        mode TEXT NOT NULL,
        held INTEGER NOT NULL DEFAULT 0,
        last TEXT CHECK (last IS NULL OR json_valid(last)),
        last_t REAL,
        drift TEXT
    ) STRICT;
    CREATE TABLE IF NOT EXISTS acts (
        id INTEGER PRIMARY KEY,
        t REAL NOT NULL,
        lever TEXT NOT NULL,
        op TEXT NOT NULL,
        params TEXT NOT NULL CHECK (json_valid(params)),
        who TEXT NOT NULL,
        why TEXT,
        mode TEXT NOT NULL,
        outcome TEXT NOT NULL,
        detail TEXT
    ) STRICT;
    CREATE INDEX IF NOT EXISTS acts_by_lever ON acts (lever, t);
    """,
    # 10: what the household wants: intents and the levels they name; and the household's
    # right to ask for things for a while and to see the plan.
    """
    CREATE TABLE IF NOT EXISTS intents (
        id TEXT PRIMARY KEY,
        body TEXT NOT NULL CHECK (json_valid(body)),
        state TEXT NOT NULL,
        created REAL NOT NULL,
        ended REAL
    ) STRICT;
    CREATE INDEX IF NOT EXISTS intents_open ON intents (ended);
    CREATE TABLE IF NOT EXISTS levels (
        id TEXT PRIMARY KEY,
        body TEXT NOT NULL CHECK (json_valid(body))
    ) STRICT;
    INSERT OR IGNORE INTO group_permissions (group_name, permission)
        SELECT 'Household', granted.value
        FROM json_each('["intent.temporary.create", "intent.temporary.create.away",
            "intent.temporary.create.guests", "plan.read"]') AS granted
        WHERE EXISTS (SELECT 1 FROM groups WHERE name = 'Household');
    """,
)
VERSION = len(MIGRATIONS)


class Transaction:
    """The database inside one transaction. Only for the function given to `run`."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._db = connection

    def get[S: Setting](self, model: type[S], id: str = "") -> S | None:
        row = self._db.execute(
            "SELECT body FROM settings WHERE kind = ? AND id = ?", (model.kind, id)
        ).fetchone()
        return None if row is None else _load(model, id, row[0])

    def all[S: Setting](self, model: type[S]) -> dict[str, S]:
        rows = self._db.execute(
            "SELECT id, body FROM settings WHERE kind = ? ORDER BY id", (model.kind,)
        )
        return {id: _load(model, id, body) for id, body in rows}

    def put(self, setting: Setting, id: str = "") -> None:
        self._db.execute(
            "INSERT INTO settings (kind, id, body, updated) VALUES (?, ?, ?, ?)"
            " ON CONFLICT (kind, id)"
            " DO UPDATE SET body = excluded.body, updated = excluded.updated",
            (setting.kind, id, setting.model_dump_json(), clock.now().isoformat()),
        )

    def delete(self, model: type[Setting], id: str = "") -> bool:
        cursor = self._db.execute(
            "DELETE FROM settings WHERE kind = ? AND id = ?", (model.kind, id)
        )
        return cursor.rowcount > 0

    def execute(self, sql: str, params: Sequence[object] = ()) -> sqlite3.Cursor:
        """Plain SQL, for the tables that aren't settings (history, and later state)."""
        return self._db.execute(sql, params)

    def executemany(self, sql: str, rows: Iterable[Sequence[object]]) -> sqlite3.Cursor:
        return self._db.executemany(sql, rows)


class Database:
    def __init__(self, executor: ThreadPoolExecutor, connection: sqlite3.Connection) -> None:
        self._executor = executor
        self._connection = connection

    @classmethod
    async def open(cls, path: Path) -> Self:
        """Open the database, creating it readable by this user only, and bring its schema
        up to date."""
        executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="thermaestro-db")
        try:
            connection = await asyncio.get_running_loop().run_in_executor(executor, _connect, path)
        except BaseException:
            executor.shutdown(wait=False)
            raise
        return cls(executor, connection)

    async def close(self) -> None:
        await self._call(self._connection.close)
        self._executor.shutdown(wait=True)

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    async def run[T](self, work: Callable[[Transaction], T]) -> T:
        """Run `work` in one transaction: everything it changes is kept, or, if it raises,
        nothing is."""
        return await self._call(_in_transaction, self._connection, work)

    async def get[S: Setting](self, model: type[S], id: str = "") -> S | None:
        return await self.run(lambda t: t.get(model, id))

    async def all[S: Setting](self, model: type[S]) -> dict[str, S]:
        return await self.run(lambda t: t.all(model))

    async def put(self, setting: Setting, id: str = "") -> None:
        await self.run(lambda t: t.put(setting, id))

    async def delete(self, model: type[Setting], id: str = "") -> bool:
        return await self.run(lambda t: t.delete(model, id))

    async def version(self) -> int:
        return await self._call(_version, self._connection)

    async def _call[T](self, fn: Callable[..., T], *args: object) -> T:
        return await asyncio.get_running_loop().run_in_executor(self._executor, fn, *args)


def _connect(path: Path) -> sqlite3.Connection:
    # This user only. SQLite gives its journal files the database file's permissions.
    with contextlib.suppress(FileExistsError):
        os.close(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))
    try:
        check_private_file(path)
    except UnsafePath as e:
        raise StoreError(str(e)) from None
    connection = sqlite3.connect(path, autocommit=True)
    try:
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = FULL")
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        _migrate(connection, path)
    except BaseException:
        connection.close()
        raise
    return connection


def _version(connection: sqlite3.Connection) -> int:
    version: int = connection.execute("PRAGMA user_version").fetchone()[0]
    return version


def _migrate(connection: sqlite3.Connection, path: Path) -> None:
    current = _version(connection)
    if current > VERSION:
        raise StoreError(
            f"{path} has schema version {current}, but this Thermaestro knows only up to "
            f"{VERSION}: it was used by a newer release"
        )
    for version in range(current + 1, VERSION + 1):
        log.info("migrating %s to schema version %d", path, version)
        try:
            connection.executescript(
                f"BEGIN IMMEDIATE;\n{MIGRATIONS[version - 1]}\n"
                f"PRAGMA user_version = {version};\nCOMMIT;"
            )
        except BaseException:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise


def _in_transaction[T](connection: sqlite3.Connection, work: Callable[[Transaction], T]) -> T:
    connection.execute("BEGIN IMMEDIATE")
    try:
        result = work(Transaction(connection))
    except BaseException:
        connection.execute("ROLLBACK")
        raise
    connection.execute("COMMIT")
    return result


def _load[S: Setting](model: type[S], id: str, body: str) -> S:
    try:
        return model.model_validate_json(body)
    except ValidationError as e:
        raise name_fields(f"setting {model.kind}{f' {id!r}' if id else ''}", e) from e
