from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from thermaestro.cap.model import Envelope, Quality
from thermaestro.core import Key, Values
from thermaestro.store import Database, Transaction

T0 = datetime(2026, 1, 15, 12, 0, tzinfo=UTC)
KEY = Key("pump", "hp1/outdoor.temp")


def env(
    value: float | str | bool | None,
    at: timedelta,
    quality: Quality = "good",
    point: str = "hp1/outdoor.temp",
) -> Envelope:
    return Envelope(
        point=point,
        value=value,
        t_observed=T0 + at,
        t_received=T0 + at,
        quality=quality,
        source="measured",
    )


@pytest.fixture
async def db(tmp_path: Path) -> AsyncIterator[Database]:
    async with await Database.open(tmp_path / "t.db") as d:
        yield d


def s(seconds: float) -> timedelta:
    return timedelta(seconds=seconds)


async def test_only_changes_and_heartbeats_are_kept(db: Database) -> None:
    values = Values(db, heartbeat_s=300)
    for at, v in [(0, 4.5), (0.5, 4.5), (1, 4.5), (2, 4.6), (2.5, 4.6), (302.5, 4.6)]:
        values.add("pump", env(v, s(at)))
    assert values.latest[KEY].value == 4.6
    assert values.pending == 3  # 4.5 at 0, 4.6 at 2, the heartbeat at 302.5
    assert await values.flush() == 3
    kept = await values.history(KEY, T0.timestamp(), T0.timestamp() + 400)
    assert [(k.t - T0.timestamp(), k.value) for k in kept] == [(0, 4.5), (2, 4.6), (302.5, 4.6)]


async def test_a_quality_change_is_kept_and_a_resent_value_is_not(db: Database) -> None:
    values = Values(db)
    values.add("pump", env(4.5, s(10)))
    values.add("pump", env(None, s(20), quality="not_connected"))
    values.add("pump", env(4.5, s(15)))  # older than what's kept
    assert values.pending == 2
    await values.flush()
    kept = await values.history(KEY, 0, 2e9)
    assert [(k.value, k.quality) for k in kept] == [(4.5, "good"), (None, "not_connected")]


async def test_text_and_bool_values(db: Database) -> None:
    values = Values(db)
    values.add("pump", env("heating", s(0), point="hp1/demand"))
    values.add("pump", env(True, s(0), point="hp1/x.fake.flag"))
    await values.flush()
    demand = await values.history(Key("pump", "hp1/demand"), 0, 2e9)
    flag = await values.history(Key("pump", "hp1/x.fake.flag"), 0, 2e9)
    assert (demand[0].text, demand[0].value, flag[0].value) == ("heating", None, 1.0)


async def test_a_failed_flush_keeps_the_rows(db: Database, monkeypatch: pytest.MonkeyPatch) -> None:
    values = Values(db)
    values.add("pump", env(4.5, s(0)))

    async def broken(work: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(db, "run", broken)
    with pytest.raises(OSError, match="disk full"):
        await values.flush()
    assert values.pending == 1
    monkeypatch.undo()
    assert await values.flush() == 1


def _slots(t: Transaction) -> list[tuple[object, ...]]:
    return list(t.execute("SELECT slot, min, mean, max, last, n FROM history_15m ORDER BY slot"))


async def test_old_samples_fold_into_15_minute_aggregates(db: Database) -> None:
    """Each value over the time it held: until the next sample, or twelve hours."""
    values = Values(db, heartbeat_s=60)
    for minute, v in enumerate([1.0, 2.0, 3.0, 9.0, 4.0]):
        values.add("pump", env(v, s(minute * 120)))  # 0, 2, 4, 6, 8 minutes: one slot
    values.add("pump", env(None, s(600), quality="not_connected"))
    values.add("pump", env(7.0, s(20 * 60)))  # the next slot
    await values.flush()
    now = T0.timestamp() + 15 * 86_400
    await values.prune(raw_days=14, aggregate_days=400, now=now)

    aggregates = await db.run(_slots)
    t0 = T0.timestamp()
    assert aggregates[:3] == [
        (t0, 1.0, 3.8, 9.0, 4.0, 5),
        (t0 + 900, 7.0, 7.0, 7.0, 7.0, 1),
        (t0 + 1800, 7.0, 7.0, 7.0, 7.0, 0),  # 7 held on
    ]
    assert aggregates[-1] == (t0 + 12 * 3600 + 900, 7.0, 7.0, 7.0, 7.0, 0)  # until 12:20
    assert len(aggregates) == 2 + 48
    assert await values.history(KEY, 0, 2e9) == []

    await values.prune(raw_days=14, aggregate_days=1, now=now)
    assert await db.run(_slots) == []


async def test_a_value_held_across_a_prune_holds_on(db: Database) -> None:
    """The newest sample before the cutoff stays, so the next fold starts from it."""
    values = Values(db, heartbeat_s=60)
    values.add("pump", env(5.0, s(0)))
    values.add("pump", env(7.0, s(3 * 3600)))
    await values.flush()
    day14 = 14 * 86_400
    await values.prune(raw_days=14, aggregate_days=400, now=T0.timestamp() + day14 + 3600)
    assert [s.value for s in await values.history(KEY, 0, 2e9)] == [5.0, 7.0]
    await values.prune(raw_days=14, aggregate_days=400, now=T0.timestamp() + day14 + 5 * 3600)
    slots = await db.run(_slots)
    assert [row[2] for row in slots] == [5.0] * 12 + [7.0] * 8
    assert [row[5] for row in slots] == [1] + [0] * 11 + [1] + [0] * 7
    assert [s.value for s in await values.history(KEY, 0, 2e9)] == [7.0]


async def test_a_mean_is_over_time_not_reports(db: Database) -> None:
    """Two hours of reports every five minutes around 20 °C, then 21 °C for six hours
    with nothing new: 20.75 °C, not the 20.04 the reports would give. The same once the
    samples are folded."""
    from zoneinfo import ZoneInfo

    zone = ZoneInfo("Europe/Stockholm")
    values = Values(db, heartbeat_s=60)
    for i in range(24):
        values.add("pump", env(19.5 if i % 2 == 0 else 20.5, s(i * 300)))
    values.add("pump", env(21.0, s(2 * 3600)))
    values.add("pump", env(None, s(8 * 3600), quality="not_connected"))
    await values.flush()
    days = await values.daily(KEY, 0, 2e9, zone)
    assert [(d.isoformat(), low, round(mean, 2), high) for d, low, mean, high in days] == [
        ("2026-01-15", 19.5, 20.75, 21.0)
    ]
    await values.prune(raw_days=14, aggregate_days=400, now=T0.timestamp() + 15 * 86_400)
    days = await values.daily(KEY, 0, 2e9, zone)
    assert [round(mean, 2) for _, _, mean, _ in days] == [20.75]


async def test_each_days_lowest_mean_and_highest(db: Database) -> None:
    """From the aggregates for older days and the samples for recent ones, by the house's
    local day: a sample at 23:30 UTC is the next day in Stockholm. A value counts in each
    day it held in."""
    from zoneinfo import ZoneInfo

    values = Values(db, heartbeat_s=60)
    for at, v in [(0, -3.0), (3600, -1.0)]:  # 12:00 and 13:00 UTC on the 15th
        values.add("pump", env(v, s(at)))
    await values.flush()
    await values.prune(raw_days=14, aggregate_days=400, now=T0.timestamp() + 15 * 86_400)
    later = T0 + timedelta(days=20)
    values.add("pump", env(2.0, later - T0))  # 12:00 UTC on 4 February: a sample still
    values.add("pump", env(-6.0, later - T0 + timedelta(hours=11, minutes=30)))  # 23:30 UTC
    await values.flush()
    days = await values.daily(KEY, 0, 2e9, ZoneInfo("Europe/Stockholm"))
    assert [(d.isoformat(), low, round(mean, 2), high) for d, low, mean, high in days] == [
        ("2026-01-15", -3.0, -1.18, -1.0),  # -3 for an hour, -1 for ten
        ("2026-01-16", -1.0, -1.0, -1.0),  # -1 until twelve hours after it was seen
        ("2026-02-04", 2.0, 2.0, 2.0),
        ("2026-02-05", -6.0, -5.68, 2.0),  # 2 until 00:30, then -6
    ]
