"""Events: raised once and cleared once, what they say kept for translation."""

from pathlib import Path

from thermaestro.core.events import KEEP_S, Events, Raise
from thermaestro.store import Database

FLOOR = Raise(
    "floor.broken", "pump:hp1/dhw", "The hot water is below its floor: %(top)s °C", {"top": 41.2}
)


async def test_raised_once_while_open(tmp_path: Path) -> None:
    async with await Database.open(tmp_path / "t.db") as db:
        events = Events(db)
        heard: list[object] = []
        events.listeners.append(heard.append)
        first = await events.raise_("floor.broken:pump:hp1/dhw", FLOOR, now=100.0)
        assert first is not None
        assert first.text() == "The hot water is below its floor: 41.2 °C"
        assert await events.raise_("floor.broken:pump:hp1/dhw", FLOOR, now=160.0) is None
        assert [e.id for e in await events.open()] == [first.id]
        # What it says changed: the same event, told again.
        lower = Raise(FLOOR.kind, FLOOR.scope, FLOOR.message, {"top": 40.1})
        changed = await events.raise_("floor.broken:pump:hp1/dhw", lower, now=220.0)
        assert changed is not None
        assert (changed.id, changed.raised, changed.changed) == (first.id, 100.0, 220.0)
        cleared = await events.clear("floor.broken:pump:hp1/dhw", now=300.0)
        assert cleared is not None
        assert cleared.cleared == 300.0
        assert await events.clear("floor.broken:pump:hp1/dhw", now=360.0) is None
        # Raised after it cleared: a new event.
        again = await events.raise_("floor.broken:pump:hp1/dhw", FLOOR, now=400.0)
        assert again is not None
        assert again.id != first.id
        assert len(heard) == 4


async def test_a_moment_and_what_was_open_since(tmp_path: Path) -> None:
    async with await Database.open(tmp_path / "t.db") as db:
        events = Events(db)
        put_back = Raise("restore.watchdog", "house", "Every setting was put back")
        await events.moment("restore.watchdog", put_back, now=50.0)
        assert await events.open() == []
        await events.raise_("floor.broken:pump:hp1/dhw", FLOOR, now=100.0)
        assert [e.kind for e in await events.since(0.0)] == ["restore.watchdog", "floor.broken"]
        assert [e.kind for e in await events.since(60.0)] == ["floor.broken"]


async def test_sync_raises_what_is_wanted_and_clears_the_rest(tmp_path: Path) -> None:
    async with await Database.open(tmp_path / "t.db") as db:
        events = Events(db)
        a = Raise("lever.drift", "pump:hp1/cs1/heating.offset", "Let go: %(why)s", {"why": "x"})
        b = Raise("lever.drift", "pump:hp1/dhw/block", "Let go: %(why)s", {"why": "y"})
        await events.sync("lever.drift:", {"lever.drift:a": a, "lever.drift:b": b}, now=10.0)
        await events.raise_("floor.broken:x", FLOOR, now=10.0)
        await events.sync("lever.drift:", {"lever.drift:b": b}, now=20.0)
        assert sorted(e.key for e in await events.open()) == ["floor.broken:x", "lever.drift:b"]
        # An underscore in a prefix is itself, not any one character.
        assert await events.open("lever_drift") == []


async def test_old_events_are_let_go(tmp_path: Path) -> None:
    async with await Database.open(tmp_path / "t.db") as db:
        events = Events(db)
        await events.raise_("floor.broken:x", FLOOR, now=0.0)
        await events.clear("floor.broken:x", now=10.0)
        await events.raise_("floor.broken:y", FLOOR, now=10.0 + KEEP_S + 1)
        assert [e.key for e in await events.since(0.0)] == ["floor.broken:y"]
