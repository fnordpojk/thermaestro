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


async def test_old_samples_fold_into_15_minute_aggregates(db: Database) -> None:
    values = Values(db, heartbeat_s=60)
    for minute, v in enumerate([1.0, 2.0, 3.0, 9.0, 4.0]):
        values.add("pump", env(v, s(minute * 120)))  # 0, 2, 4, 6, 8 minutes: one slot
    values.add("pump", env(None, s(600), quality="not_connected"))
    values.add("pump", env(7.0, s(20 * 60)))  # the next slot
    await values.flush()
    now = T0.timestamp() + 15 * 86_400
    await values.prune(raw_days=14, aggregate_days=400, now=now)

    def rows(t: Transaction) -> list[tuple[object, ...]]:
        return list(
            t.execute("SELECT slot, min, mean, max, last, n FROM history_15m ORDER BY slot")
        )

    aggregates = await db.run(rows)
    assert aggregates == [
        (T0.timestamp(), 1.0, 3.8, 9.0, 4.0, 5),
        (T0.timestamp() + 900, 7.0, 7.0, 7.0, 7.0, 1),
    ]
    assert await values.history(KEY, 0, 2e9) == []

    await values.prune(raw_days=14, aggregate_days=1, now=now)
    assert await db.run(rows) == []
