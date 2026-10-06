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
from datetime import UTC, datetime
from pathlib import Path
from typing import Self

from pydantic import ValidationError

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
            (setting.kind, id, setting.model_dump_json(), datetime.now(UTC).isoformat()),
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
