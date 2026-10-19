"""The planner's memory between rounds outlasts a restart: whole within the hour, and after a
longer stop, what still holds without the price holds a stop has ended."""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

from thermaestro.core import AuditLog, Values
from thermaestro.learn import Learnings
from thermaestro.planner import Planner
from thermaestro.planner.model import FRESH_S, Effect, Memory
from thermaestro.store import Database

NOW = datetime(2026, 10, 16, 0, 20, tzinfo=UTC)
TANK = "pump:hp1/dhw"
CS = "pump:hp1/cs1"


def planner(db: Database, tmp_path: Path) -> Planner:
    nothing = cast(Any, None)
    return Planner(db, Values(db), nothing, nothing, nothing, nothing, AuditLog(tmp_path / "audit"))


def remembered() -> Memory:
    return Memory(
        integral={CS: -14.0},
        last_round=NOW - timedelta(minutes=15),
        periodic={TANK: NOW - timedelta(days=3)},
        charge_s={TANK: 846.0},
        blocked={TANK: True},
        blocked_since={TANK: NOW - timedelta(hours=2)},
        fired={"in-boost": NOW - timedelta(minutes=5)},
        charging_since={TANK: NOW - timedelta(minutes=4), f"{TANK}#on": NOW},
        drift={CS: 0.4},
    )


async def test_a_restart_carries_on_from_the_memory(tmp_path: Path) -> None:
    async with await Database.open(tmp_path / "t.db") as db:
        before = planner(db, tmp_path)
        before.memory = remembered()
        await before.keep_memory(NOW.timestamp())
        after = planner(db, tmp_path)
        await after.restore_memory(NOW.timestamp() + 120)
        assert after.memory == remembered()


async def test_after_a_long_stop_the_price_holds_are_over(tmp_path: Path) -> None:
    async with await Database.open(tmp_path / "t.db") as db:
        before = planner(db, tmp_path)
        before.memory = remembered()
        await before.keep_memory(NOW.timestamp())
        after = planner(db, tmp_path)
        await after.restore_memory(NOW.timestamp() + FRESH_S + 60)
        kept = after.memory
        assert kept.integral == {CS: -14.0}
        assert kept.periodic == remembered().periodic
        assert kept.charge_s == {TANK: 846.0}
        assert kept.drift == {CS: 0.4}
        assert kept.fired == remembered().fired  # a boost asked for isn't asked for again
        assert (kept.blocked, kept.blocked_since, kept.last_round) == ({}, {}, None)
        assert kept.charging_since == {}


async def test_nothing_kept_starts_afresh(tmp_path: Path) -> None:
    async with await Database.open(tmp_path / "t.db") as db:
        fresh = planner(db, tmp_path)
        await fresh.restore_memory(NOW.timestamp())
        assert fresh.memory == Memory()


async def test_the_optimizer_shares_the_drift_kept(tmp_path: Path) -> None:
    async with await Database.open(tmp_path / "t.db") as db:
        before = planner(db, tmp_path)
        before.memory = remembered()
        await before.keep_memory(NOW.timestamp())
        learnings = Learnings(db, AuditLog(tmp_path / "learn-audit"))
        await learnings.load()
        nothing = cast(Any, None)
        after = Planner(
            db,
            Values(db),
            nothing,
            nothing,
            nothing,
            nothing,
            AuditLog(tmp_path / "a"),
            learnings=learnings,
        )
        await after.restore_memory(NOW.timestamp() + 60)
        assert after.optimizer is not None
        assert after.optimizer.drift is after.memory.drift
        assert after.optimizer.drift == {CS: 0.4}


async def test_the_short_cycle_guard_airing_and_waits_outlast_a_restart(tmp_path: Path) -> None:
    """When the compressor last stopped, rooms seen aired, settings the pump didn't keep and
    effects waited for: within the hour all of it; after a longer stop, not the waits."""
    t = NOW.timestamp()
    kept = Memory(
        stopped=t - 300,
        aired={"room.hall/temperature": t - 600},
        not_kept={'pump:hp1/cs1/heating.offset set {"value": 2}': t + 1000},
        effects=[Effect("pump:hp1/dhw/boost", "boost now, as asked", NOW, 900.0)],
    )
    async with await Database.open(tmp_path / "t.db") as db:
        before = planner(db, tmp_path)
        before.memory = kept
        await before.keep_memory(t)
        after = planner(db, tmp_path)
        await after.restore_memory(t + 120)
        assert after.memory == kept
        later = planner(db, tmp_path)
        await later.restore_memory(t + FRESH_S + 60)
        assert later.memory.effects == []  # a wait a stop ended isn't judged
        assert later.memory.stopped == t - 300
        assert later.memory.aired == kept.aired
