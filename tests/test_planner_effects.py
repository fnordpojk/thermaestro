"""What the planner waits for after a change, and how it judges it: a boost by the charge it
starts, a block by the charge it holds off, until its time or its release. A release waits
for nothing of its own."""

import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pytest

from thermaestro.cap.model import Envelope, Lever, Value, Verify
from thermaestro.core import AuditLog, Values
from thermaestro.core.executor import GONE, LOST, Result
from thermaestro.planner import Planner
from thermaestro.planner.model import Decision, Effect, Memory
from thermaestro.store import Database

NOW = datetime(2026, 11, 22, 3, 30, tzinfo=UTC)
BLOCK = "pump:hp1/dhw/block"
BOOST = "pump:hp1/dhw/boost_once"
TANK = "pump:hp1/dhw"


class NoIntents:
    async def all(self, **_: Any) -> list[Any]:
        return []


def planner(db: Database, tmp_path: Path) -> Planner:
    nothing = cast(Any, None)
    p = Planner(db, Values(db), nothing, nothing, nothing, nothing, AuditLog(tmp_path / "audit"))
    p._intents = cast(Any, NoIntents())
    return p


def reads(p: Planner, point: str, value: Value, at: datetime = NOW) -> None:
    p._values.add(
        "pump",
        Envelope(
            point=point,
            value=value,
            t_observed=at,
            t_received=at,
            quality="good",
            source="measured",
        ),
    )


def judged(tmp_path: Path) -> list[dict[str, Any]]:
    lines = (tmp_path / "audit").glob("*")
    entries = [json.loads(line) for f in lines for line in f.read_text().splitlines()]
    return [e for e in entries if e["what"] == "lever.effect"]


async def test_a_block_is_judged_by_the_charge_it_holds_off(tmp_path: Path) -> None:
    async with await Database.open(tmp_path / "t.db") as db:
        p = planner(db, tmp_path)
        reads(p, "hp1/demand", "idle")
        reads(p, "hp1/dhw/temp.charge", 55.3)
        p.memory.effects = [Effect(BLOCK, "held off until a cheap hour", NOW, 900.0, "none")]
        await p._follow_up(NOW + timedelta(minutes=10))
        assert judged(tmp_path) == []  # not yet its time
        await p._follow_up(NOW + timedelta(minutes=16))
        [entry] = judged(tmp_path)
        assert entry["outcome"] == "ok"
        assert entry["details"]["expects"] == "none"
        assert p.memory.effects == []


async def test_a_charge_under_a_block_is_said_with_the_charge_sensor(tmp_path: Path) -> None:
    async with await Database.open(tmp_path / "t.db") as db:
        p = planner(db, tmp_path)
        reads(p, "hp1/demand", "dhw")
        reads(p, "hp1/dhw/temp.charge", 34.8)
        p.memory.effects = [Effect(BLOCK, "held off until a cheap hour", NOW, 900.0, "none")]
        await p._follow_up(NOW + timedelta(minutes=5))
        [entry] = judged(tmp_path)
        assert entry["outcome"] == "charged"
        assert entry["details"]["charge_sensor"] == 34.8


async def test_a_charge_that_ran_between_rounds_is_seen(tmp_path: Path) -> None:
    async with await Database.open(tmp_path / "t.db") as db:
        p = planner(db, tmp_path)
        reads(p, "hp1/demand", "idle")
        p.memory.effects = [Effect(BLOCK, "held off until a cheap hour", NOW, 900.0, "none")]
        p.memory.charging_since[TANK] = NOW + timedelta(minutes=3)
        await p._follow_up(NOW + timedelta(minutes=15))
        assert [e["outcome"] for e in judged(tmp_path)] == ["charged"]


async def test_a_boost_is_judged_by_the_charge_it_starts(tmp_path: Path) -> None:
    async with await Database.open(tmp_path / "t.db") as db:
        p = planner(db, tmp_path)
        reads(p, "hp1/demand", "idle")
        p.memory.effects = [Effect(BOOST, "boost now, as asked", NOW, 900.0)]
        await p._follow_up(NOW + timedelta(minutes=16))
        reads(p, "hp1/demand", "dhw")
        p.memory.effects = [Effect(BOOST, "boost now, as asked", NOW, 900.0)]
        await p._follow_up(NOW + timedelta(minutes=2))
        assert [e["outcome"] for e in judged(tmp_path)] == ["failed", "ok"]


class Acting:
    """An executor that accepts every change and waits for its effect; the levers are holds
    or triggers by name."""

    def lever(self, ref: str) -> Lever:
        kind = "hold" if ref == BLOCK else "trigger"
        path = ref.split(":", 1)[1]
        return Lever(path=path, kind=kind, verify=Verify(kind="none"), touches=("x.nibe.47044",))

    def __init__(self) -> None:
        self.answers: dict[str, Result] = {}
        """What the executor answers an op (or a restore), where not `awaiting_effect`."""
        self.meanwhile: list[Callable[[], None]] = []
        """What happens while a change is under way, before the executor answers."""

    def _answer(self, op: str) -> Result:
        for happen in self.meanwhile:
            happen()
        return self.answers.get(op, Result("awaiting_effect", None))

    async def act(self, ref: str, op: str, params: dict[str, Any], **_: Any) -> Result:
        return self._answer(op)

    async def restore(self, why: str, refs: list[str]) -> dict[str, Result]:
        return {ref: self._answer("restore") for ref in refs}


async def test_a_release_waits_for_nothing_and_an_engage_for_no_charge(tmp_path: Path) -> None:
    async with await Database.open(tmp_path / "t.db") as db:
        p = planner(db, tmp_path)
        p._executor = cast(Any, Acting())
        released = Decision(BLOCK, "release", {}, 1, "a cheap hour")
        await p._ask(released, NOW)
        assert p.memory.effects == []
        engaged = Decision(BLOCK, "engage", {}, 1, "held off until a cheap hour")
        await p._ask(engaged, NOW)
        assert [e.expects for e in p.memory.effects] == ["none"]


@pytest.mark.parametrize("op", ["release", "restore"])
async def test_a_release_ends_what_the_engage_waited_for(tmp_path: Path, op: str) -> None:
    """Released for a cheap hour's charge, or put back: the engage is judged up to then,
    and the charge after isn't put down to it."""
    async with await Database.open(tmp_path / "t.db") as db:
        p = planner(db, tmp_path)
        p._executor = cast(Any, Acting())
        await p._ask(Decision(BLOCK, "engage", {}, 1, "held off until a cheap hour"), NOW)
        await p._ask(Decision(BLOCK, op, {}, 1, "a cheap hour"), NOW + timedelta(minutes=1))
        assert p.memory.effects == []
        [entry] = judged(tmp_path)
        assert (entry["outcome"], entry["why"]) == ("ok", "held off until a cheap hour")
        assert (entry["details"]["released"], entry["details"]["after_s"]) == (True, 60)
        reads(p, "hp1/demand", "dhw")
        await p._follow_up(NOW + timedelta(minutes=2))
        assert len(judged(tmp_path)) == 1


@pytest.mark.parametrize("op", ["release", "restore"])
@pytest.mark.parametrize(
    "answer",
    [
        Result("device_refused", None),
        Result("timeout", "no acceptance received"),
        Result("lost", LOST),
        Result("unverifiable", GONE),
    ],
    ids=["refused", "timeout", "lost", "gone"],
)
async def test_a_release_not_taken_leaves_the_engage_waiting(
    tmp_path: Path, op: str, answer: Result
) -> None:
    """The pump refused the release, or what became of it isn't known: the block may still
    hold, and a charge now is one it let through."""
    async with await Database.open(tmp_path / "t.db") as db:
        p = planner(db, tmp_path)
        acting = Acting()
        acting.answers[op] = answer
        p._executor = cast(Any, acting)
        await p._ask(Decision(BLOCK, "engage", {}, 1, "held off until a cheap hour"), NOW)
        await p._ask(Decision(BLOCK, op, {}, 1, "a cheap hour"), NOW + timedelta(minutes=1))
        assert [e.expects for e in p.memory.effects] == ["none"]
        assert judged(tmp_path) == []
        reads(p, "hp1/demand", "dhw")
        await p._follow_up(NOW + timedelta(minutes=2))
        assert [e["outcome"] for e in judged(tmp_path)] == ["charged"]


@pytest.mark.parametrize(("started", "outcome"), [(1, "ok"), (2, "ok"), (0.5, "charged")])
async def test_a_charge_is_put_down_to_the_block_only_if_it_started_before_the_release(
    tmp_path: Path, started: float, outcome: str
) -> None:
    """The release is asked at minute 1. A charge seen starting as it is under way, before
    the executor answers, is the release's; one already running when it was asked is the
    block's."""
    async with await Database.open(tmp_path / "t.db") as db:
        p = planner(db, tmp_path)
        acting = Acting()
        p._executor = cast(Any, acting)
        reads(p, "hp1/demand", "idle")
        await p._ask(Decision(BLOCK, "engage", {}, 1, "held off until a cheap hour"), NOW)
        at = NOW + timedelta(minutes=started)
        acting.meanwhile.append(lambda: reads(p, "hp1/demand", "dhw", at))
        await p._ask(Decision(BLOCK, "release", {}, 1, "a cheap hour"), NOW + timedelta(minutes=1))
        assert p.memory.effects == []
        assert [e["outcome"] for e in judged(tmp_path)] == [outcome]


async def test_a_charge_that_ran_once_the_release_was_asked_is_the_releases(
    tmp_path: Path,
) -> None:
    """A whole charge, started after the release was asked and over before the executor
    answered, isn't put down to the block either."""
    async with await Database.open(tmp_path / "t.db") as db:
        p = planner(db, tmp_path)
        acting = Acting()
        p._executor = cast(Any, acting)
        reads(p, "hp1/demand", "idle")
        await p._ask(Decision(BLOCK, "engage", {}, 1, "held off until a cheap hour"), NOW)
        ran = NOW + timedelta(minutes=2)
        acting.meanwhile.append(lambda: p.memory.charging_since.__setitem__(TANK, ran))
        await p._ask(Decision(BLOCK, "release", {}, 1, "a cheap hour"), NOW + timedelta(minutes=1))
        assert [e["outcome"] for e in judged(tmp_path)] == ["ok"]


def test_what_is_expected_is_kept_through_a_restart() -> None:
    kept = Memory(effects=[Effect(BLOCK, "held", NOW, 900.0, "none"), Effect(BOOST, "b", NOW, 1.0)])
    again = Memory.from_body(kept.body(), fresh=True)
    assert [e.expects for e in again.effects] == ["none", "charge"]
    older = kept.body()
    for e in older["effects"]:
        del e["expects"]  # kept by a version before this
    assert [e.expects for e in Memory.from_body(older, fresh=True).effects] == ["charge"] * 2
