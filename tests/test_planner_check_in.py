"""What the planner says as it checks in: the holds it manages, from what it last looked at,
and a fresh look when a tank comes or goes, as when the pump's plugin comes up."""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from thermaestro import clock
from thermaestro.core import AuditLog, Values
from thermaestro.planner import Planner
from thermaestro.planner.model import SLOT
from thermaestro.store import Database

NOW = datetime(2026, 11, 23, 22, 33, 10, tzinfo=UTC)
TANK = "pump:hp1/dhw"
BLOCK = f"{TANK}/block"


class Checking:
    """An executor that keeps what the planner says as it checks in."""

    def __init__(self) -> None:
        self.managed: list[tuple[frozenset[str], float | None]] = []
        self.beats = 0

    def manages(self, refs: set[str], seen: float | None = None) -> None:
        self.managed.append((frozenset(refs), seen))

    def heartbeat(self) -> None:
        self.beats += 1


class Nothing:
    async def all(self, **_: Any) -> list[Any]:
        return []

    async def due(self, *_: Any) -> None:
        return None


def looked(tanks: set[str], at: datetime) -> Any:
    return SimpleNamespace(tanks=dict.fromkeys(tanks), now=at, zone="Europe/Stockholm")


def planner(db: Database, tmp_path: Path, there: set[str]) -> tuple[Planner, Checking]:
    nothing = cast(Any, None)
    p = Planner(db, Values(db), nothing, nothing, nothing, nothing, AuditLog(tmp_path / "audit"))
    checking = Checking()
    p._executor = cast(Any, checking)
    p._intents = cast(Any, Nothing())
    p.summaries = cast(Any, Nothing())
    p._house = cast(Any, SimpleNamespace(capabilities=SimpleNamespace(tanks=dict.fromkeys(there))))
    p._last_slot = NOW.timestamp() // SLOT.total_seconds()
    p._last_intents = frozenset()
    return p, checking


@pytest.fixture(autouse=True)
def frozen(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(clock, "now", lambda: NOW)


async def test_the_planner_says_its_holds_as_of_when_it_last_looked(tmp_path: Path) -> None:
    async with await Database.open(tmp_path / "t.db") as db:
        p, checking = planner(db, tmp_path, {TANK})
        earlier = NOW - timedelta(minutes=5)
        p.situation = looked({TANK}, earlier)
        assert await p.check() is None  # nothing new: no round
        assert checking.managed == [(frozenset({BLOCK}), earlier.timestamp())]
        assert checking.beats == 1


async def test_a_tank_come_since_the_last_look_is_looked_at_again(tmp_path: Path) -> None:
    """The last look was before the pump's plugin came up, without its tank: the planner
    looks again, a whole round, and says it manages the tank's block as of then."""
    async with await Database.open(tmp_path / "t.db") as db:
        p, checking = planner(db, tmp_path, {TANK})
        p.situation = looked(set(), NOW - timedelta(minutes=1))
        rounds: list[bool] = []
        planned = cast(Any, object())

        async def round_(*, slot: bool = True) -> Any:
            rounds.append(slot)
            p.situation = looked({TANK}, NOW)
            return planned

        p.round = round_  # type: ignore[method-assign]
        assert await p.check() is planned
        assert rounds == [True]
        assert checking.managed == [(frozenset({BLOCK}), NOW.timestamp())]
