"""The planner's memory between rounds outlasts a restart: whole within the hour, and after a
longer stop, what still holds without the price holds a stop has ended."""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

from thermaestro.core import AuditLog, Values
from thermaestro.intents import Calendar, Capabilities, Resolver
from thermaestro.learn import Learnings
from thermaestro.planner import LeverState, Planner, Situation, Tank
from thermaestro.planner.model import FRESH_S, Effect, Memory
from thermaestro.planner.rules import BLOCK_MOST
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
        held_since={TANK: NOW - timedelta(hours=2)},
        spent={f"{TANK}2": NOW - timedelta(hours=1)},
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
        # A block held through the stop counts its time from before it: let go once it has
        # held the longest, not held that long again.
        assert (kept.held_since, kept.spent) == (remembered().held_since, remembered().spent)
        assert (kept.blocked, kept.blocked_since, kept.last_round) == ({}, {}, None)
        assert kept.charging_since == {}


async def test_how_long_a_block_holds_is_kept_whichever_planner_holds_it(tmp_path: Path) -> None:
    block = f"{TANK}/block"

    def sit(at: datetime, *, held: bool, demand: str | None = None) -> Situation:
        return Situation(
            now=at,
            zone=UTC,
            force=Resolver([], {}, Calendar("UTC")).in_force(at),
            ahead={},
            deadlines=[],
            caps=Capabilities(tanks=frozenset({TANK})),
            prices=[],
            rooms=[],
            tanks={TANK: Tank(TANK, 48.0, 35.0, 45.0)},
            levers={block: LeverState(block, "hold", held=held)},
            demand=demand,
        )

    async with await Database.open(tmp_path / "t.db") as db:
        p = planner(db, tmp_path)
        await p._observe(sit(NOW, held=True))
        await p._observe(sit(NOW + timedelta(hours=2), held=True))
        assert p.memory.held_since == {TANK: NOW}
        assert p.memory.spent == {}
        await p._observe(sit(NOW + BLOCK_MOST, held=True))
        assert p.memory.spent == {TANK: NOW + BLOCK_MOST}
        await p._observe(sit(NOW + BLOCK_MOST + timedelta(minutes=15), held=False))
        assert (p.memory.held_since, TANK in p.memory.spent) == ({}, True)  # until a charge
        await p._observe(sit(NOW + BLOCK_MOST + timedelta(minutes=30), held=False, demand="dhw"))
        assert p.memory.spent == {}


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


async def test_an_ordinary_charge_isnt_taken_for_the_periodic_increase(tmp_path: Path) -> None:
    """After a charge in a warmer mode, the top stands well above economy's stop: the
    periodic increase is seen only from the charge sensor at its own stop."""

    def sit(at: datetime, tank: Tank) -> Situation:
        return Situation(
            now=at,
            zone=UTC,
            force=Resolver([], {}, Calendar("UTC")).in_force(at),
            ahead={},
            deadlines=[],
            caps=Capabilities(tanks=frozenset({TANK})),
            prices=[],
            rooms=[],
            tanks={TANK: tank},
            levers={},
        )

    economy = Tank(TANK, 48.5, 41.0, 45.0, charge=44.0, periodic_stop=55.0)
    async with await Database.open(tmp_path / "t.db") as db:
        p = planner(db, tmp_path)
        await p._observe(sit(NOW, economy))
        assert p.memory.periodic == {}
        periodic = Tank(TANK, 59.5, 41.0, 45.0, charge=54.5, periodic_stop=55.0)
        await p._observe(sit(NOW + timedelta(hours=1), periodic))
        assert p.memory.periodic == {TANK: NOW + timedelta(hours=1)}
