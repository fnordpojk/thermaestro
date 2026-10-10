import json
import sqlite3
import stat
from contextlib import closing
from pathlib import Path

import pytest

from thermaestro.store import (
    VERSION,
    Database,
    Home,
    Location,
    Mqtt,
    PriceLayer,
    Sensor,
    StoreError,
    Transaction,
    Vat,
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


async def test_the_brokers_old_discovery_switch_is_dropped(tmp_path: Path) -> None:
    path = tmp_path / "thermaestro.db"
    async with await Database.open(path) as db:
        await db.put(Mqtt(host="192.0.2.30"))
    with closing(sqlite3.connect(path, autocommit=True)) as raw:
        raw.execute(
            "UPDATE settings SET body = json_set(body, '$.discovery', json('true'))"
            " WHERE kind = 'mqtt'"
        )
        raw.execute("PRAGMA user_version = 6")
    async with await Database.open(path) as db:
        assert await db.get(Mqtt) == Mqtt(host="192.0.2.30")


async def test_emitters_taken_from_the_pump_are_forgotten(tmp_path: Path) -> None:
    path = tmp_path / "thermaestro.db"
    async with await Database.open(path) as db:
        await db.put(Home(emitters={"pump:hp1/cs1": "radiators"}, water="well"))
    with closing(sqlite3.connect(path, autocommit=True)) as raw:
        raw.execute("PRAGMA user_version = 11")
    async with await Database.open(path) as db:
        assert await db.get(Home) == Home(water="well")


async def test_underfloor_heating_answered_before_the_split_is_a_slab(tmp_path: Path) -> None:
    path = tmp_path / "thermaestro.db"
    async with await Database.open(path):
        pass
    old = {
        "emitters": {
            "pump:hp1/cs1": "floor",
            "pump:hp1/cs2": "radiators_and_floor",
            "pump:hp1/cs3": "radiators",
        },
        "water": "well",
    }
    with closing(sqlite3.connect(path, autocommit=True)) as raw:
        raw.execute(
            "INSERT INTO settings (kind, id, body, updated) VALUES ('home', '', ?, '2026-10-10')",
            (json.dumps(old),),
        )
        raw.execute("PRAGMA user_version = 15")
    async with await Database.open(path) as db:
        home = await db.get(Home)
    assert home is not None
    assert home.emitters == {
        "pump:hp1/cs1": "slab",
        "pump:hp1/cs2": "radiators_and_slab",
        "pump:hp1/cs3": "radiators",
    }
    assert set(home.check_emitters) == {"pump:hp1/cs1", "pump:hp1/cs2"}
    # Run again (as after a restore): nothing more changes.
    with closing(sqlite3.connect(path, autocommit=True)) as raw:
        raw.execute("PRAGMA user_version = 15")
    async with await Database.open(path) as db:
        assert await db.get(Home) == home


async def test_a_stored_remainder_is_a_suppliers_total_again(tmp_path: Path) -> None:
    path = tmp_path / "thermaestro.db"
    spot = PriceLayer(
        role="energy.spot",
        source="series",
        plugin="entsoe",
        series="spot",
        unit="SEK/kWh",
        vat="excl",
    )
    async with await Database.open(path) as db:
        await db.put(spot, "energy-spot")
        await db.put(spot.model_copy(update={"series": "total"}), "energy-supplier")
        await db.put(Vat(rate=0.25, applies_to=("energy-spot", "energy-supplier", "tax.energy")))
    with closing(sqlite3.connect(path, autocommit=True)) as raw:
        # As the release that stored them wrote them: every layer with `minus`, the
        # supplier's as a remainder.
        raw.execute(
            "UPDATE settings SET body = json_set(body, '$.minus', json('null'))"
            " WHERE kind = 'price.layer'"
        )
        raw.execute(
            "UPDATE settings SET body = json_set(body, '$.source', 'remainder',"
            " '$.plugin', 'tibber', '$.minus', 'tibber:energy', '$.role', 'energy.supplier')"
            " WHERE kind = 'price.layer' AND id = 'energy-supplier'"
        )
        raw.execute("PRAGMA user_version = 7")
    async with await Database.open(path) as db:
        layers = await db.all(PriceLayer)
        assert layers["energy-spot"] == spot
        supplier = layers["energy-supplier"]
        assert (supplier.source, supplier.plugin, supplier.series, supplier.vat) == (
            "series",
            "tibber",
            "total",
            "incl",
        )
        vat = await db.get(Vat)
        assert vat is not None
        assert vat.applies_to == ("energy-spot", "tax.energy")


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
