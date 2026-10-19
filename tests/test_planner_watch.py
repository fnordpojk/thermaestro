"""What the house shows, kept as events: each raised once while it holds, cleared after."""

from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import cast

from test_planner_rules import NOW, ROOM, TANK, band, floor, situation

from thermaestro import clock
from thermaestro.cap.messages import DeviceEvent, Health
from thermaestro.core.events import Events
from thermaestro.core.executor import Claim, Executor
from thermaestro.core.host import Instance, PluginHost, State
from thermaestro.planner import Situation
from thermaestro.planner.watch import MISSED_S, MISSING_S, Watch
from thermaestro.store import Database, Plugin


def watching(db: Database, claims: dict[str, Claim] | None = None, **instances: Instance) -> Watch:
    executor = cast(Executor, SimpleNamespace(claims=claims or {}))
    host = cast(PluginHost, SimpleNamespace(instances=instances))
    return Watch(Events(db), executor, host)


def instance(id: str, state: State = State.UP, health: Health | None = None) -> Instance:
    setting = Plugin(plugin="nibe", settings={"host": "192.0.2.10"})
    return Instance(id, setting, state=state, health=health)


async def keys(db: Database) -> list[str]:
    return sorted(e.key for e in await Events(db).open())


def later_by(sit: Situation, seconds: float) -> Situation:
    return replace(sit, now=sit.now + timedelta(seconds=seconds))


async def test_a_broken_floor_is_raised_once_and_cleared(tmp_path: Path) -> None:
    async with await Database.open(tmp_path / "t.db") as db:
        watch = watching(db)
        await watch.look(situation([floor(42.0)], top=41.0))
        await watch.look(situation([floor(42.0)], top=40.5))  # colder: the same event
        events = await Events(db).open()
        assert [(e.key, e.text()) for e in events] == [
            (f"floor.broken:{TANK}", "The hot water fell below its floor of 42.0 °C")
        ]
        assert events[0].changed == events[0].raised
        await watch.look(situation([floor(42.0)], top=44.0))
        assert await keys(db) == []


async def test_rooms_without_a_fresh_temperature(tmp_path: Path) -> None:
    async with await Database.open(tmp_path / "t.db") as db:
        watch = watching(db)
        await watch.look(situation([], temp=None))
        assert await keys(db) == ["room.stale:pump:hp1/cs1"]
        await watch.look(situation([]))
        assert await keys(db) == []


async def test_a_setting_let_go(tmp_path: Path) -> None:
    async with await Database.open(tmp_path / "t.db") as db:
        claim = Claim(
            "pump:hp1/cs1/heating.offset", 0.0, 0, "control", drift="set to 3 at the pump"
        )
        await watching(db, {claim.lever: claim}).look(situation([]))
        [event] = await Events(db).open()
        assert event.text() == "pump:hp1/cs1/heating.offset was let go: set to 3 at the pump"


async def test_a_plugin_missing_for_over_an_hour(tmp_path: Path) -> None:
    async with await Database.open(tmp_path / "t.db") as db:
        late = Health(t=NOW, state="up", stale=("spot",))
        prices = instance("prices", health=late)
        watch = watching(db, pump=instance("pump", State.RESTARTING), prices=prices)
        await watch.look(situation([]))
        assert await keys(db) == []  # not yet an hour
        later = later_by(situation([]), MISSING_S)
        await watch.look(later)
        assert await keys(db) == ["plugin.missing:prices", "plugin.missing:pump"]
        texts = sorted(e.text() for e in await Events(db).open())
        assert texts == [
            "prices hasn't published spot for over an hour",
            "pump has been restarting for over an hour",
        ]
        # Back, then gone again: an hour from then.
        prices.health = Health(t=NOW, state="up")
        await watch.look(later_by(later, 60))
        prices.health = late
        await watch.look(later_by(later, 120))
        assert await keys(db) == ["plugin.missing:pump"]


async def test_a_devices_warning_while_it_is_on(tmp_path: Path) -> None:
    async with await Database.open(tmp_path / "t.db") as db:
        pump = instance("pump")
        warning = DeviceEvent(
            t=NOW,
            unit="hp1",
            code="brine.out.low",
            active=True,
            text="Brine out is near the pump's alarm limit",
        )
        pump.active[("hp1", "brine.out.low")] = warning
        watch = watching(db, pump=pump)
        await watch.look(situation([]))
        [event] = await Events(db).open()
        assert event.text() == "pump: Brine out is near the pump's alarm limit"
        pump.active.clear()
        await watch.look(situation([]))
        assert await keys(db) == []


async def test_a_room_below_its_band_for_over_half_an_hour(tmp_path: Path) -> None:
    async with await Database.open(tmp_path / "t.db") as db:
        watch = watching(db)
        warm = situation([band()], temp=21.25)
        await watch.look(warm)
        reach = timedelta(hours=8)  # past the response its emitters take
        cold = later_by(situation([band()], temp=20.0), reach.total_seconds())
        await watch.look(cold)
        assert await keys(db) == []  # below, but not for half an hour yet
        await watch.look(later_by(cold, MISSED_S))
        [event] = await Events(db).open()
        assert (event.key, event.audience) == (f"comfort.missed:{ROOM}", "household")
        assert event.text() == "living has been below its 20.5 °C for over half an hour"
        await watch.look(later_by(situation([band()], temp=20.6), reach.total_seconds() + 3600))
        assert await keys(db) == []


async def test_a_room_isnt_missed_before_its_heat_could_show(tmp_path: Path) -> None:
    async with await Database.open(tmp_path / "t.db") as db:
        watch = watching(db)
        cold = situation([band()], temp=20.0)  # first seen: as if the band had just risen
        await watch.look(cold)
        await watch.look(later_by(cold, MISSED_S + 60))
        assert await keys(db) == []


async def test_a_clock_set_right_is_no_time_passed(tmp_path: Path) -> None:
    """The system clock set forward two hours (by NTP, after long without it): the timers
    move with it, so a plugin restarting for a minute isn't missing for over an hour."""

    class Jumping:
        def __init__(self) -> None:
            self.wall, self.mono = NOW.timestamp(), 0.0

        def time(self) -> float:
            return self.wall

        def monotonic(self) -> float:
            return self.mono

    source = Jumping()
    async with await Database.open(tmp_path / "t.db") as db:
        watch = watching(db, pump=instance("pump", State.RESTARTING))
        with clock.simulated(source):
            await watch.look(situation([]))
            source.wall += 2 * 3600
            source.mono += 60
            await watch.look(later_by(situation([]), 2 * 3600))
        assert await keys(db) == []
