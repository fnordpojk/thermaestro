import sqlite3
import stat
from contextlib import closing
from pathlib import Path

import pytest

from thermaestro.store import (
    VERSION,
    Database,
    Location,
    Mqtt,
    Sensor,
    StoreError,
    Transaction,
)

HOME = Location(latitude=52.52, longitude=13.40, timezone="Europe/Berlin")


def mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


async def test_a_new_database_is_migrated_from_empty(tmp_path: Path) -> None:
    path = tmp_path / "thermaestro.db"
    async with await Database.open(path) as db:
        assert await db.version() == VERSION
        assert await db.all(Sensor) == {}
    with closing(sqlite3.connect(path)) as raw:
        tables = raw.execute("SELECT name FROM sqlite_schema").fetchall()
    assert ("settings",) in tables


async def test_migrations_run_once(tmp_path: Path) -> None:
    path = tmp_path / "thermaestro.db"
    async with await Database.open(path) as db:
        await db.put(HOME)
    async with await Database.open(path) as db:
        assert await db.version() == VERSION
        assert await db.get(Location) == HOME


async def test_a_newer_database_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "thermaestro.db"
    async with await Database.open(path):
        pass
    with closing(sqlite3.connect(path, autocommit=True)) as raw:
        raw.execute(f"PRAGMA user_version = {VERSION + 1}")
    with pytest.raises(StoreError, match="newer release"):
        await Database.open(path)


async def test_the_files_are_this_users_only(tmp_path: Path) -> None:
    path = tmp_path / "thermaestro.db"
    async with await Database.open(path) as db:
        await db.put(HOME)
        assert mode(path) == 0o600
        assert mode(tmp_path / "thermaestro.db-wal") == 0o600


async def test_a_database_open_to_others_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "thermaestro.db"
    path.touch(mode=0o644)
    path.chmod(0o644)
    with pytest.raises(StoreError, match="open to others"):
        await Database.open(path)


async def test_settings_round_trip(tmp_path: Path) -> None:
    async with await Database.open(tmp_path / "t.db") as db:
        living = Sensor(name="Living room", source="mqtt", topic="home/living/t")
        await db.put(living, "living")
        await db.put(HOME)
        assert await db.get(Sensor, "living") == living
        assert await db.all(Sensor) == {"living": living}
        assert await db.get(Mqtt) is None
        assert await db.delete(Sensor, "living")
        assert not await db.delete(Sensor, "living")


async def test_a_transaction_is_all_or_nothing(tmp_path: Path) -> None:
    async with await Database.open(tmp_path / "t.db") as db:

        def half_done(t: Transaction) -> None:
            t.put(HOME)
            raise RuntimeError("crash halfway")

        with pytest.raises(RuntimeError):
            await db.run(half_done)
        assert await db.get(Location) is None


async def test_a_bad_stored_setting_names_the_field(tmp_path: Path) -> None:
    path = tmp_path / "t.db"
    async with await Database.open(path) as db:
        await db.put(HOME)
    with closing(sqlite3.connect(path, autocommit=True)) as raw:
        raw.execute("UPDATE settings SET body = json_set(body, '$.latitude', 123)")
    async with await Database.open(path) as db:
        with pytest.raises(StoreError, match=r"setting location: latitude: "):
            await db.get(Location)
