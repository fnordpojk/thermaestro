"""The daily summary: made at 07:00 for the day before, once; the pump's electricity from its
meter, what it cost, a temporary intent's share, the minutes a room was below its band,
the writes and the compressor's starts. It waits for the pump and the day's prices, and
makes the days missed while Thermaestro wasn't running."""

import json
from datetime import UTC, date, datetime, timedelta, tzinfo
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
from thermaestro.store import Database, PriceLayer, Sensor
from thermaestro.store.database import MIGRATIONS

DAY = datetime(2026, 10, 15, tzinfo=UTC)
T0 = DAY.timestamp()
T1 = T0 + 86_400
COMPRESSOR = Key("pump", "hp1/compressor.ep14/state")
METER = Key(SENSORS, "meter/energy")
PUMPS = [Pump("pump:hp1", COMPRESSOR)]
MORNING = DAY + timedelta(days=1, hours=7)


def at(hours: float) -> float:
    return T0 + hours * 3600


async def prices(day: date, zone: tzinfo) -> list[Price]:
    """1 a kWh the first half of the day, 2 the second."""
    start = datetime.combine(day, datetime.min.time(), zone)
    return [Price(start + timedelta(minutes=15 * i), 1.0 if i < 48 else 2.0) for i in range(96)]


async def unpublished(day: date, zone: tzinfo) -> list[Price]:
    return []


async def house(db: Database, days: int = 1) -> Events:
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
    # The meter: a kWh an hour, read every minute, from the day's start.
    counter = [(METER, at(m / 60), m / 60, None) for m in range(days * 24 * 60 + 2)]
    # The compressor: two runs.
    ran = [(at(h), state) for h, state in [(0, "running"), (1, "off")]]
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


def summaries(db: Database, events: Events, pumps: list[Pump] = PUMPS, priced=prices) -> Summaries:  # type: ignore[no-untyped-def]
    return Summaries(db, Values(db), events, None, lambda: pumps, priced)


async def test_the_day_before_summed_up_at_seven(tmp_path: Path) -> None:
    async with await Database.open(tmp_path / "t.db") as db:
        events = await house(db)
        found = summaries(db, events)
        assert await found.due(MORNING - timedelta(minutes=1), UTC) == []
        [made] = await found.due(MORNING, UTC)
        assert await found.due(MORNING + timedelta(minutes=5), UTC) == []
        assert await found.recent(7) == [made]
        assert made["day"] == "2026-10-15"
        assert made["kwh"] == pytest.approx(24.0, abs=0.1)
        assert made["energy_from"] == "meter"
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
        assert await summaries(db, events).due(DAY - timedelta(hours=17), UTC) == []
        assert await summaries(db, events).recent(7) == []


async def test_it_waits_for_the_pump_and_the_days_prices(tmp_path: Path) -> None:
    async with await Database.open(tmp_path / "t.db") as db:
        events = await house(db)
        spot = PriceLayer(
            role="energy.spot",
            source="series",
            plugin="tibber",
            series="spot",
            unit="SEK/kWh",
            vat="excl",
        )
        await db.put(spot, "spot")
        pumps: list[Pump] = []
        found = summaries(db, events, pumps, unpublished)
        assert await found.due(MORNING, UTC) == []  # the pump hasn't described itself yet
        pumps.extend(PUMPS)
        assert await found.due(MORNING + timedelta(minutes=1), UTC) == []  # nor the prices
        [made] = await found.due(MORNING + timedelta(hours=5), UTC)  # noon: without them
        assert made["cost"] is None
        assert made["kwh"] == pytest.approx(24.0, abs=0.1)
        assert made["starts"] == 2


async def test_days_missed_while_it_was_off_are_made(tmp_path: Path) -> None:
    async with await Database.open(tmp_path / "t.db") as db:
        events = await house(db, days=3)
        made = await summaries(db, events).due(MORNING + timedelta(days=2), UTC)
        assert [m["day"] for m in made] == ["2026-10-15", "2026-10-16", "2026-10-17"]
        assert [m["kwh"] for m in made] == pytest.approx([24.0, 24.0, 24.0], abs=0.1)


async def test_unmeasured_energy_is_unknown_not_none_used(tmp_path: Path) -> None:
    async with await Database.open(tmp_path / "t.db") as db:
        events = await house(db)
        await db.run(lambda t: t.execute("DELETE FROM settings WHERE kind = 'sensor'"))
        [made] = await summaries(db, events).due(MORNING, UTC)
        assert (made["kwh"], made["cost"]) == (None, None)
        assert [t["kwh"] for t in made["temporary"]] == [None]


async def test_without_a_meter_or_a_learned_draw_the_makers_figures(tmp_path: Path) -> None:
    async with await Database.open(tmp_path / "t.db") as db:
        events = await house(db)
        await db.run(lambda t: t.execute("DELETE FROM settings WHERE kind = 'sensor'"))
        rated = [Pump("pump:hp1", COMPRESSOR, {"heating": 1.5, "dhw": 2.0})]
        [made] = await summaries(db, events, rated).due(MORNING, UTC)
        assert made["energy_from"] == "maker"
        assert made["kwh"] == pytest.approx(3.0, abs=0.05)  # two hours' heating at 1.5 kW
        assert made["cost"] == pytest.approx(3.0, abs=0.05)  # all of it in the first half


async def test_summaries_made_before_the_pump_was_known_are_made_again(tmp_path: Path) -> None:
    async with await Database.open(tmp_path / "t.db") as db:
        rows = [("2026-10-15", {"starts": None, "kwh": 0.0}), ("2026-10-16", {"starts": 9})]

        def keep(t) -> None:  # type: ignore[no-untyped-def]
            for day, body in rows:
                t.execute(
                    "INSERT INTO summaries (day, made, body) VALUES (?, 0, ?)",
                    (day, json.dumps(body)),
                )
            t.execute(MIGRATIONS[23])

        await db.run(keep)
        left = await db.run(lambda t: t.execute("SELECT day FROM summaries").fetchall())
        assert [d for (d,) in left] == ["2026-10-16"]
