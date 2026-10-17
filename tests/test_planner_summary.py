"""The daily summary: made at 07:00 for the day before, once; the pump's electricity from its
meter, what it cost, a temporary intent's share, the minutes a room was below its band,
the writes and the compressor's starts."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from thermaestro.core import Values
from thermaestro.core.events import Events, Raise
from thermaestro.core.sensors import SENSORS
from thermaestro.core.values import Key
from thermaestro.intents import kinds
from thermaestro.planner.model import Price
from thermaestro.planner.summary import Pump, Summaries
from thermaestro.planner.watch import COMFORT_MISSED
from thermaestro.store import Database, Sensor

DAY = datetime(2026, 10, 15, tzinfo=UTC)
T0 = DAY.timestamp()
T1 = T0 + 86_400
COMPRESSOR = Key("pump", "hp1/compressor.ep14/state")
METER = Key(SENSORS, "meter/energy")


def at(hours: float) -> float:
    return T0 + hours * 3600


async def prices(now: datetime) -> list[Price]:
    """1 a kWh the first half of the day, 2 the second."""
    return [Price(DAY + timedelta(minutes=15 * i), 1.0 if i < 48 else 2.0) for i in range(96)]


async def house(db: Database) -> Events:
    await db.put(
        Sensor(
            name="Heat pump",
            source="mqtt",
            topic="house/energy/heatpump",
            quantity="energy",
            placement="other",
            meters="pump:hp1",
        ),
        "meter",
    )
    # The meter: a kWh an hour, read every minute, from the hour before the day.
    counter = [(METER, at(m / 60), m / 60 + 1, None) for m in range(-60, 24 * 60 + 2)]
    # The compressor: two runs.
    ran = [(at(h), state) for h, state in [(-1, "off"), (0, "running"), (1, "off")]]
    ran += [(at(h), state) for h, state in [(3, "running"), (4, "off")]]
    state = [(COMPRESSOR, t, None, text) for t, text in ran]
    await db.run(
        lambda t: t.executemany(
            "INSERT INTO history (instance, point, t, value, text, quality)"
            " VALUES (?, ?, ?, ?, ?, 'good')",
            [(k.instance, k.point, s, v, text) for k, s, v, text in counter + state],
        )
    )
    # Three writes the planner had sent; one the guard stopped, one a person's, one the next day.
    acts = [(at(1), "planner", "verified")] * 3
    acts += [(at(2), "planner", "refused"), (at(2), "user:test", "verified")]
    acts += [(T1 + 60, "planner", "verified")]
    await db.run(
        lambda t: t.executemany(
            "INSERT INTO acts (t, lever, op, params, who, mode, outcome)"
            " VALUES (?, 'pump:hp1/cs1/heating.offset', 'set', '{}', ?, 'control', ?)",
            acts,
        )
    )
    # Two degrees warmer from 05:00 to 07:00.
    made = datetime.fromtimestamp(at(5), UTC)
    until = datetime.fromtimestamp(at(7), UTC)
    warmer = kinds.warmer("pump:hp1/cs1", 2.0, principal="user:test", created=made, until=until)
    finished = warmer.model_copy(update={"state": "finished"})
    await db.run(
        lambda t: t.execute(
            "INSERT INTO intents (id, body, state, created, ended) VALUES (?, ?, ?, ?, ?)",
            (finished.id, finished.model_dump_json(), "finished", at(5), at(7)),
        )
    )
    # The living room below its band from 09:50 (raised half an hour on) to 10:20.
    events = Events(db)
    below = Raise(
        "comfort.missed",
        "room:living",
        COMFORT_MISSED,
        {"room": "Living room", "low": 20.5},
        audience="household",
    )
    await events.raise_("comfort.missed:room:living", below, now=at(10 + 20 / 60))
    await events.clear("comfort.missed:room:living", now=at(10 + 50 / 60))
    return events


def summaries(db: Database, events: Events) -> Summaries:
    return Summaries(db, Values(db), events, None, lambda: [Pump("pump:hp1", COMPRESSOR)], prices)


async def test_the_day_before_summed_up_at_seven(tmp_path: Path) -> None:
    async with await Database.open(tmp_path / "t.db") as db:
        events = await house(db)
        found = summaries(db, events)
        next_day = DAY + timedelta(days=1)
        assert await found.due(next_day + timedelta(hours=6, minutes=59), UTC) is None
        made = await found.due(next_day + timedelta(hours=7), UTC)
        assert made is not None
        assert await found.due(next_day + timedelta(hours=7, minutes=5), UTC) is None
        assert await found.recent(7) == [made]
        assert made["day"] == "2026-10-15"
        assert made["kwh"] == pytest.approx(24.0, abs=0.1)
        assert made["cost"] == pytest.approx(36.0, abs=0.2)
        [warmer] = made["temporary"]
        assert warmer["kind"] == "warmer"
        assert warmer["kwh"] == pytest.approx(2.0, abs=0.1)
        assert warmer["cost"] == pytest.approx(2.0, abs=0.1)
        assert [(i["kind"], i["state"]) for i in made["ended"]] == [("warmer", "finished")]
        assert made["rooms"] == [{"name": "Living room", "minutes": 60}]
        assert made["writes"] == 3
        assert made["starts"] == 2
        told = await events.since(0.0, "summary.daily:")
        assert [(e.key, e.audience) for e in told] == [("summary.daily:2026-10-15", "household")]
        assert json.loads(json.dumps(made)) == made  # kept as it was made


async def test_a_day_nothing_was_seen_in_is_not_summed_up(tmp_path: Path) -> None:
    async with await Database.open(tmp_path / "t.db") as db:
        events = await house(db)
        assert await summaries(db, events).due(DAY - timedelta(hours=17), UTC) is None
        assert await summaries(db, events).recent(7) == []
