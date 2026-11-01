"""Errors that would only reach the log, raised as events: what failed, the error and an
ID; cleared when the part works again; a moment at most once an hour."""

import logging
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from thermaestro import clock
from thermaestro.core import AuditLog, Values
from thermaestro.core.events import Events
from thermaestro.core.failures import PREFIX, Failures, attached_for_tests, fault_id
from thermaestro.core.values import Key
from thermaestro.learn import Learnings
from thermaestro.learn.hotwater import HotWater, TankKeys
from thermaestro.store import Database


def _raised(message: str) -> Exception:
    try:
        raise RuntimeError(message)
    except RuntimeError as e:
        return e


async def test_a_learner_that_fails_is_an_event_until_it_works(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    async with await Database.open(tmp_path / "t.db") as db:
        events = Events(db)
        keys = TankKeys(
            "dev:hp1/dhw",
            Key("dev", "hp1/dhw/temp.top"),
            Key("dev", "hp1/dhw/temp.charge"),
            Key("dev", "hp1/demand"),
            Key("dev", "hp1/dhw/temp.stop"),
        )
        learnings = Learnings(db, AuditLog(tmp_path / "audit"))
        await learnings.load()
        host = cast(Any, SimpleNamespace(instances={}))
        hot = HotWater(db, Values(db), host, learnings, lambda key: True)
        cast(Any, hot).tanks = lambda: iter([keys])

        async def broken(*args: Any) -> None:
            raise KeyError("draws")

        with attached_for_tests(events) as failures:
            monkeypatch.setattr(hot, "_learn", broken)
            with caplog.at_level(logging.ERROR):
                await hot.run()
            await failures.settle()
            [event] = await events.open(PREFIX)
            assert event.key == "failure:learn.tank:dev:hp1/dhw"
            assert event.audience == "technical"
            assert event.args["what_label"] == "Learning a hot-water tank"
            assert event.args["where"] == "dev:hp1/dhw"
            assert event.args["error"] == "KeyError: 'draws'"
            # The same ID in the log line, beside the traceback.
            [record] = [r for r in caplog.records if event.args["id"] in r.getMessage()]
            assert record.exc_info is not None
            await hot.run()  # failing the same way again says nothing new
            await failures.settle()
            assert [e.id for e in await events.open(PREFIX)] == [event.id]

            async def works(*args: Any) -> None:
                return None

            monkeypatch.setattr(hot, "_learn", works)
            await hot.run()
            await failures.settle()
            assert await events.open(PREFIX) == []


async def test_a_moment_is_told_at_most_once_an_hour(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = [2_000_000_000.0]
    monkeypatch.setattr(clock, "time", lambda: now[0])
    async with await Database.open(tmp_path / "t.db") as db:
        events = Events(db)
        with attached_for_tests(events) as failures:
            for _ in range(3):
                failures.failed(
                    "shutdown", "A step of stopping Thermaestro", _raised("no"), moment=True
                )
            now[0] += 3601
            failures.failed(
                "shutdown", "A step of stopping Thermaestro", _raised("no"), moment=True
            )
            await failures.settle()
            told = await events.since(0.0, PREFIX)
            assert len(told) == 2
            assert all(not e.open for e in told)


async def test_a_failure_left_open_clears_after_a_restart_once_it_works(tmp_path: Path) -> None:
    async with await Database.open(tmp_path / "t.db") as db:
        events = Events(db)
        with attached_for_tests(events) as failures:
            failures.failed("planning", "Planning", _raised("no"))
            await failures.settle()
        restarted = Failures()
        await restarted.attach(events)  # a new process: what is open is known again
        restarted.worked("planning")
        await restarted.settle()
        assert await events.open(PREFIX) == []


async def test_a_clear_that_couldnt_be_kept_is_tried_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The part works, but the clear can't be written: the failure stays open until the
    part next works, then clears, with no restart."""
    async with await Database.open(tmp_path / "t.db") as db:
        events = Events(db)
        with attached_for_tests(events) as failures:
            failures.failed("history", "Writing the value history", _raised("no"))
            await failures.settle()
            clear, broken = events.clear, [True]

            async def once_broken(key: str, **kw: Any) -> Any:
                if broken[0]:
                    broken[0] = False
                    raise sqlite3.OperationalError("database is locked")
                return await clear(key, **kw)

            monkeypatch.setattr(events, "clear", once_broken)
            failures.worked("history")
            await failures.settle()
            assert [e.key for e in await events.open(PREFIX)] == ["failure:history"]
            failures.worked("history")
            await failures.settle()
            assert await events.open(PREFIX) == []


async def test_a_failure_raised_after_a_lost_clear_stays_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Worked, then failed again before the clear was written, and the clear lost: the
    newer failure is what stands, and it too clears once the part works."""
    async with await Database.open(tmp_path / "t.db") as db:
        events = Events(db)
        with attached_for_tests(events) as failures:
            failures.failed("history", "Writing the value history", _raised("no"))
            await failures.settle()
            clear = events.clear
            broken = [True]

            async def once_broken(key: str, **kw: Any) -> Any:
                if broken[0]:
                    broken[0] = False
                    raise sqlite3.OperationalError("database is locked")
                return await clear(key, **kw)

            monkeypatch.setattr(events, "clear", once_broken)
            failures.worked("history")
            failures.failed("history", "Writing the value history", _raised("again"))
            await failures.settle()
            [event] = await events.open(PREFIX)
            assert event.args["error"] == "RuntimeError: again"
            failures.worked("history")
            await failures.settle()
            assert await events.open(PREFIX) == []


def test_the_same_fault_has_the_same_id() -> None:
    def fail() -> Exception:
        return _raised("one")

    assert fault_id(fail()) == fault_id(fail())
    assert fault_id(fail()) != fault_id(KeyError("x"))
