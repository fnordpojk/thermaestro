"""A plugin instance's own store: what it keeps across restarts."""

from pathlib import Path

from thermaestro.core.plugins import PluginStore
from thermaestro.store import Database


async def test_each_instance_keeps_its_own(tmp_path: Path) -> None:
    async with await Database.open(tmp_path / "t.db") as db:
        pump, other = PluginStore(db, "pump"), PluginStore(db, "pump2")
        assert await pump.load() == {}
        await pump.save({"meters": {"42437": {"words": [0, 3483], "idle": 120.5}}})
        await other.save({"x": 1})
    async with await Database.open(tmp_path / "t.db") as db:
        assert await PluginStore(db, "pump").load() == {
            "meters": {"42437": {"words": [0, 3483], "idle": 120.5}}
        }
        assert await PluginStore(db, "pump2").load() == {"x": 1}
