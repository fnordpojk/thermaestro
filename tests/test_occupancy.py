"""Whether anyone is home, from the alarm, presence sensors and hot water drawn: concluded,
kept, and acted on by nothing yet."""

import time
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from thermaestro.cap.model import Envelope
from thermaestro.core.sensors import SensorHub
from thermaestro.core.values import Key, Values
from thermaestro.learn.hotwater import TankKeys
from thermaestro.occupancy import QUIET_ONE_S, QUIET_S, SPREAD_S, Occupancy
from thermaestro.store import Database, Sensor

ALARM = "ha:alarm_control_panel.house"
SOFA = "ha:binary_sensor.sofa"
STEP_S = 120.0


class Clock:
    def __init__(self) -> None:
        self.now = time.time() - 3 * 86_400  # room behind for a history

    def __call__(self) -> float:
        return self.now


def envelope(point: str, value: object, t: float, quality: str = "good") -> Envelope:
    at = datetime.fromtimestamp(t, UTC)
    return Envelope.model_validate(
        {
            "point": point,
            "value": value,
            "t_observed": at,
            "t_received": at,
            "quality": quality,
            "source": "measured",
            "why": "unavailable" if quality != "good" else None,
        }
    )


@dataclass
class House:
    db: Database
    values: Values
    hub: SensorHub
    clock: Clock
    up: dict[str, bool]

    def ha(self, point: str, value: object) -> None:
        self.values.add("ha", envelope(point.removeprefix("ha:"), value, self.clock.now))

    def unavailable(self, point: str) -> None:
        """What Home Assistant says of an entity it can't reach."""
        self.values.add("ha", envelope(point.removeprefix("ha:"), None, self.clock.now, "unknown"))

    async def run(self, occupancy: Occupancy, seconds: float, step: float = STEP_S) -> None:
        end = self.clock.now + seconds
        while self.clock.now < end:
            self.clock.now += step
            self.hub.tick()
            await occupancy.tick()


@pytest.fixture
async def db(tmp_path: Path) -> AsyncIterator[Database]:
    async with await Database.open(tmp_path / "t.db") as db:
        yield db


async def house(
    db: Database, *, alarm: bool = True, sofa: bool = True, at: float | None = None
) -> House:
    if alarm:
        await db.put(
            Sensor(
                name="Alarm", source="point", point=ALARM, quantity="alarm_panel", placement="other"
            ),
            "alarm",
        )
    if sofa:
        await db.put(
            Sensor(
                name="Sofa", source="point", point=SOFA, quantity="occupancy", placement="other"
            ),
            "sofa",
        )
    clock = Clock()
    if at is not None:
        clock.now = at
    up = {"ha": True}
    values = Values(db)
    hub = SensorHub(db, values, clock=clock, up=lambda instance: up.get(instance, False))
    await hub.load()
    return House(db, values, hub, clock, up)


async def started(
    h: House, *, alarm: str | None = None, sofa: bool | None = None, **kw: Any
) -> Occupancy:
    """The service started on a house whose alarm and sofa sensor say `alarm` and `sofa`
    already: states from before it started, not changes it sees."""
    if alarm is not None:
        h.ha(ALARM, alarm)
    if sofa is not None:
        h.ha(SOFA, sofa)
    h.clock.now += 1
    occupancy = Occupancy(h.db, h.values, h.hub, clock=h.clock, **kw)
    await occupancy.load()
    h.clock.now += 1
    return occupancy


def state(occupancy: Occupancy) -> str:
    """What is concluded now; read anew each time, as the house runs."""
    return str(occupancy.now.state)


async def kept(db: Database) -> list[tuple[str, str]]:
    rows = await db.run(lambda t: list(t.execute("SELECT state, why FROM occupancy ORDER BY id")))
    return [(state, why) for state, why in rows]


# --- the alarm --------------------------------------------------------------------------------


async def test_the_alarm_armed_for_away_is_nobody_home_at_once(db: Database) -> None:
    h = await house(db)
    occupancy = await started(h, alarm="disarmed", sofa=False)
    await h.run(occupancy, 600)
    armed = h.clock.now
    h.ha(ALARM, "armed_away")
    await h.run(occupancy, STEP_S)
    assert (occupancy.now.state, occupancy.now.why) == ("away", "alarm")
    assert occupancy.now.began == pytest.approx(armed)
    h.ha(SOFA, True)  # the cat: a person would have set the alarm off
    await h.run(occupancy, 3600)
    assert state(occupancy) == "away"
    disarmed = h.clock.now
    h.ha(ALARM, "disarmed")
    await h.run(occupancy, STEP_S)
    assert (occupancy.now.state, occupancy.now.why) == ("home", "disarmed")
    assert occupancy.now.args["at"] == pytest.approx(disarmed)
    assert await kept(db) == [("away", "alarm"), ("home", "disarmed")]


async def test_armed_for_the_night_is_someone_home(db: Database) -> None:
    h = await house(db)
    occupancy = await started(h, alarm="armed_night", sofa=False)
    await h.run(occupancy, STEP_S)
    assert (occupancy.now.state, occupancy.now.why) == ("home", "alarm_home")
    await h.run(occupancy, 13 * 3600)
    assert state(occupancy) == "home"  # quiet all night: people asleep, not away


async def test_a_short_gap_doesnt_end_an_absence_the_alarm_showed(db: Database) -> None:
    """Home Assistant restarting leaves the alarm not known for a while: the absence stands
    if the alarm is still armed for away once known again."""
    h = await house(db)
    occupancy = await started(h, alarm="armed_away", sofa=False)
    await h.run(occupancy, STEP_S)
    assert state(occupancy) == "away"
    h.up["ha"] = False
    await h.run(occupancy, 600)
    assert state(occupancy) == "away"
    assert not occupancy.sources["alarm"].known
    h.up["ha"] = True
    h.ha(ALARM, "armed_away")  # back, it sends every state again
    h.ha(SOFA, False)
    await h.run(occupancy, 600)
    assert (occupancy.now.state, occupancy.now.why) == ("away", "alarm")
    assert await kept(db) == [("away", "alarm")]  # one absence, not two


# --- quiet ------------------------------------------------------------------------------------


async def test_quiet_for_twelve_hours_with_every_source_known_is_nobody_home(db: Database) -> None:
    h = await house(db)
    occupancy = await started(h, alarm="disarmed", sofa=False)
    moved = h.clock.now
    h.ha(SOFA, True)
    await h.run(occupancy, STEP_S)
    h.ha(SOFA, False)
    await h.run(occupancy, QUIET_S - 600)
    assert state(occupancy) != "away"
    await h.run(occupancy, 1200)
    assert (occupancy.now.state, occupancy.now.why) == ("away", "quiet")
    assert occupancy.now.args["hours"] == 12
    assert occupancy.now.began == pytest.approx(moved)


async def test_a_source_not_known_starts_the_window_again(db: Database) -> None:
    """No evidence is not evidence of nobody: a quiet hour with Home Assistant away doesn't
    count, and the window starts again once it is back."""
    h = await house(db)
    occupancy = await started(h, alarm="disarmed", sofa=False)
    await h.run(occupancy, 8 * 3600)
    h.up["ha"] = False
    await h.run(occupancy, 3600)
    h.up["ha"] = True
    h.ha(ALARM, "disarmed")  # what it sends again: the same, so no change seen
    h.ha(SOFA, False)
    await h.run(occupancy, 8 * 3600)
    assert state(occupancy) != "away"  # 16 quiet hours, but not 12 in a row known
    await h.run(occupancy, 4.5 * 3600)
    assert state(occupancy) == "away"


async def test_a_house_with_a_single_source_needs_a_day(db: Database) -> None:
    h = await house(db, alarm=False)
    occupancy = await started(h, sofa=False)
    await h.run(occupancy, QUIET_S + 3600)
    assert state(occupancy) != "away"
    await h.run(occupancy, QUIET_ONE_S - QUIET_S)
    assert (occupancy.now.state, occupancy.now.args["hours"]) == ("away", 24)


async def test_a_house_with_no_source_is_never_concluded_empty(db: Database) -> None:
    h = await house(db, alarm=False, sofa=False)
    occupancy = await started(h)
    await h.run(occupancy, 2 * 86_400)
    assert (occupancy.now.state, occupancy.now.why) == ("unknown", "no_sources")


@dataclass
class Bound:
    value: object


class Quiet:
    """Quiet hours from 22:00 to 07:00 UTC, as the household's intents give them."""

    async def in_force(self, at: datetime) -> "Quiet":
        self._at = at
        return self

    def bound(self, target: str, scope: str) -> Bound | None:
        night = self._at.hour >= 22 or self._at.hour < 7
        return Bound(True) if target == "quiet" and scope == "house" and night else None


async def test_the_households_quiet_hours_are_not_counted(db: Database) -> None:
    """Asleep is not away. From 18:00: four hours to 22:00, none in the night, and eight
    more from 07:00, so not before 15:00 the next day, 21 hours on."""
    h = await house(db, at=datetime(2026, 11, 2, 18, 0, tzinfo=UTC).timestamp())
    occupancy = await started(h, alarm="disarmed", sofa=False, intents=Quiet())
    await h.run(occupancy, 15 * 3600)
    assert state(occupancy) != "away"  # 12 hours on, but six counted
    await h.run(occupancy, 6.5 * 3600)
    assert state(occupancy) == "away"


# --- coming back --------------------------------------------------------------------------------


async def test_one_trigger_is_a_moment_activity_over_half_an_hour_is_home(db: Database) -> None:
    h = await house(db)
    occupancy = await started(h, alarm="disarmed", sofa=False)
    await h.run(occupancy, QUIET_S + 600)
    assert state(occupancy) == "away"
    first = h.clock.now
    h.ha(SOFA, True)
    await h.run(occupancy, STEP_S)
    h.ha(SOFA, False)
    await h.run(occupancy, STEP_S)
    assert state(occupancy) == "away"
    assert occupancy.seen_since == pytest.approx(first)
    await h.run(occupancy, SPREAD_S)
    h.ha(SOFA, True)
    await h.run(occupancy, STEP_S)
    assert (occupancy.now.state, occupancy.now.why) == ("home", "activity")
    assert occupancy.now.began == pytest.approx(first)
    assert occupancy.seen_since is None


async def test_a_sensor_that_stays_on_is_someone_there_all_along(db: Database) -> None:
    """Someone on the sofa all evening is there all evening: never quiet while it is on,
    home once it has been on for half an hour, and quiet counted from when it goes off."""
    h = await house(db)
    occupancy = await started(h, alarm="disarmed", sofa=False)
    sat = h.clock.now
    h.ha(SOFA, True)
    await h.run(occupancy, SPREAD_S + STEP_S)
    assert (occupancy.now.state, occupancy.now.why) == ("home", "activity")
    assert occupancy.now.began == pytest.approx(sat, abs=1)
    await h.run(occupancy, QUIET_S + 3600)
    assert state(occupancy) == "home"
    assert occupancy.sources["presence"].known
    up = h.clock.now
    h.ha(SOFA, False)
    await h.run(occupancy, QUIET_S - 600)
    assert state(occupancy) == "home"
    await h.run(occupancy, 1200)
    assert (occupancy.now.state, occupancy.now.why) == ("away", "quiet")
    assert occupancy.now.began == pytest.approx(up, abs=1)


async def test_a_sensor_on_since_before_the_start_is_someone_there_now(db: Database) -> None:
    """On, and still on: its state is what it is now, however long ago it turned on."""
    h = await house(db)
    occupancy = await started(h, alarm="disarmed", sofa=True)
    await h.run(occupancy, QUIET_S + 3600)
    assert (occupancy.now.state, occupancy.now.why) == ("home", "activity")


async def test_a_sensor_not_known_is_neither_someone_nor_quiet(db: Database) -> None:
    h = await house(db)
    occupancy = await started(h, alarm="disarmed", sofa=False)
    await h.run(occupancy, QUIET_S + 600)
    assert state(occupancy) == "away"
    h.unavailable(SOFA)
    await h.run(occupancy, 3600)
    assert not occupancy.sources["presence"].known
    assert state(occupancy) == "away"
    assert occupancy.seen_since is None  # not known isn't someone
    h.ha(SOFA, True)  # back, and on
    await h.run(occupancy, SPREAD_S + STEP_S)
    assert (occupancy.now.state, occupancy.now.why) == ("home", "activity")
    h.unavailable(SOFA)  # gone again, last seen on
    await h.run(occupancy, QUIET_S + 3600)
    assert state(occupancy) == "home"  # no quiet counted while it isn't known
    h.ha(SOFA, False)  # back, and off
    await h.run(occupancy, QUIET_S - 600)
    assert state(occupancy) == "home"
    await h.run(occupancy, 1200)
    assert (occupancy.now.state, occupancy.now.why) == ("away", "quiet")


class Tank:
    """A tank whose readings are kept in the history, and found usable."""

    scope = "pump:hp1/dhw"
    keys = TankKeys(
        scope,
        Key("pump", "hp1/dhw/temp.top"),
        Key("pump", "hp1/dhw/temp.charge"),
        Key("pump", "hp1/demand"),
        Key("pump", "hp1/dhw/temp.stop"),
    )

    def tanks(self) -> Iterator[TankKeys]:
        yield self.keys


async def tank_history(h: House, start: float, end: float, drawn_at: float | None) -> None:
    """The tank at 50 °C, falling 6 °C over half an hour at `drawn_at`; the pump idle."""
    t = start
    while t <= end:
        fall = 0.0
        if drawn_at is not None and t >= drawn_at:
            fall = min(6.0, (t - drawn_at) / 1800 * 6.0)
        for key in (Tank.keys.top, Tank.keys.low):
            h.values.add("pump", envelope(key.point, 50.0 - fall, t))
        h.values.add("pump", envelope(Tank.keys.demand.point, "idle", t))
        t += 300
    await h.values.flush()


async def test_a_draw_is_someone_home(db: Database) -> None:
    h = await house(db, alarm=False, sofa=False)
    await tank_history(h, h.clock.now - 2 * 86_400, h.clock.now, None)
    occupancy = await started(h, hot_water=Tank(), usable=lambda key: True)
    await h.run(occupancy, STEP_S)
    assert occupancy.sources["draws"].known
    assert state(occupancy) == "unknown"
    drawn = h.clock.now + 600
    await tank_history(h, h.clock.now, h.clock.now + 4 * 3600, drawn)
    await h.run(occupancy, 4 * 3600)
    assert (occupancy.now.state, occupancy.now.why) == ("home", "draw")
    assert occupancy.now.args["at"] == pytest.approx(drawn, abs=1800)


async def test_a_tank_not_read_is_a_source_not_known(db: Database) -> None:
    h = await house(db, alarm=False, sofa=False)
    occupancy = await started(h, hot_water=Tank(), usable=lambda key: False)
    await h.run(occupancy, STEP_S)
    assert not occupancy.sources["draws"].known


# --- kept ---------------------------------------------------------------------------------------


async def test_a_restart_keeps_the_conclusion_and_starts_the_window_again(db: Database) -> None:
    h = await house(db)
    occupancy = await started(h, alarm="disarmed", sofa=False)
    await h.run(occupancy, QUIET_S + 600)
    assert state(occupancy) == "away"
    await occupancy.stop()
    h.clock.now += 3600  # Thermaestro down an hour
    again = await started(h)
    assert (again.now.state, again.now.why) == ("away", "quiet")
    await h.run(again, 600)
    assert state(again) == "away"
    assert await kept(db) == [("away", "quiet")]


async def test_readings_remembered_at_a_restart_are_not_new(db: Database) -> None:
    """At a start the sensors' last readings are given again, as old as they are: the sofa
    turning on before the restart isn't someone turning up now. Home Assistant, back, says
    it went off meanwhile."""
    h = await house(db)
    occupancy = await started(h, alarm="disarmed", sofa=False)
    await h.run(occupancy, QUIET_S + 600)
    assert state(occupancy) == "away"
    sat = h.clock.now
    h.ha(SOFA, True)  # before the restart
    await h.run(occupancy, STEP_S)
    await occupancy.stop()
    await h.values.flush()
    h.clock.now += 600
    values = Values(db)  # Thermaestro started again
    hub = SensorHub(db, values, clock=h.clock, up=lambda instance: True)
    again = Occupancy(db, values, hub, clock=h.clock)
    await again.load()
    h.clock.now += 1
    await hub.load()  # the last readings, given again
    values.add("ha", envelope(SOFA.removeprefix("ha:"), False, sat + 300))
    h.clock.now += STEP_S
    await again.tick()
    assert state(again) == "away"
    assert again.seen_since is None


async def test_a_draw_during_a_gap_ends_an_absence_the_alarm_showed(db: Database) -> None:
    """Home Assistant away, the alarm not known: hot water drawn meanwhile is someone home."""
    h = await house(db)
    await tank_history(h, h.clock.now - 2 * 86_400, h.clock.now, None)
    occupancy = await started(
        h, alarm="armed_away", sofa=False, hot_water=Tank(), usable=lambda key: True
    )
    await h.run(occupancy, STEP_S)
    assert state(occupancy) == "away"
    h.up["ha"] = False
    drawn = h.clock.now + 600
    await tank_history(h, h.clock.now, h.clock.now + 2 * 3600, drawn)
    await h.run(occupancy, 2 * 3600)
    assert (state(occupancy), occupancy.now.why) == ("home", "draw")


def test_nothing_heats_less_for_an_absence_yet() -> None:
    """The house learns the household's heat from it, the tank what is drawn by kind of day,
    and the plans expect who is home ahead with them (the optimizer, and the planner for the
    tank's draws); but no band, deadline or rule reads it."""
    root = Path(__file__).parents[1] / "src" / "thermaestro"
    for part in ("planner", "intents", "optimize"):
        for source in (root / part).rglob("*.py"):
            if source.name in ("optimizer.py", "service.py"):
                continue
            text = source.read_text()
            assert "occupancy" not in text, source
            assert "occupied" not in text, source
