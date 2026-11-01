"""A proof stands until a test with enough to judge by fails: too little since to judge a
model again keeps it proved; a failed test, or other inputs, doesn't."""

from pathlib import Path
from typing import Any

import pytest

from thermaestro import clock
from thermaestro.core import AuditLog
from thermaestro.learn import Learned, Learnings, electric, heating, hotwater, house
from thermaestro.learn.validate import FEW, NOT_BETTER, PROVED, UNJUDGED
from thermaestro.store import Database

DRAW = "compressor.draw"


def _draw(state: Any, why: str, value: dict[str, float] | None, **more: Any) -> Learned:
    return Learned(
        name=DRAW, scope="dev:hp1", kind="model", value=value, state=state, why=why, **more
    )


async def test_a_proof_stands_until_a_test_with_enough_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = [2_000_000_000.0]
    monkeypatch.setattr(clock, "time", lambda: now[0])
    async with await Database.open(tmp_path / "t.db") as db:
        learnings = Learnings(db, AuditLog(tmp_path / "audit"))
        await learnings.load()
        args = {"error": 0.09, "baseline": 0.2}
        await learnings.put(_draw("proved", PROVED, {"heating": 1.9}, args=args, n=30))
        proved_at = now[0]
        now[0] += 86_400  # a warm day: the compressor hardly ran
        kept = await learnings.put(
            _draw("learning", FEW, {"heating": 2.0}, args={"n": 12, "least": 24}, n=12)
        )
        assert kept.state == "proved"
        from thermaestro.learn.store import KEPT_PROOF

        assert (kept.why, kept.args) == (KEPT_PROOF, {"seen": proved_at})
        assert kept.value == {"heating": 2.0}  # the newest value, still used
        assert learnings.used(DRAW, "dev:hp1") == {"heating": 2.0}
        now[0] += 86_400  # still too little: when it was proved stays as it was
        again = await learnings.put(_draw("learning", FEW, None, args={"n": 3, "least": 24}))
        assert again.args == {"seen": proved_at}
        assert again.value == {"heating": 2.0}  # nothing new: the last one kept
        now[0] += 86_400  # judged, and not better: it isn't used any more
        failed = await learnings.put(
            _draw("learning", NOT_BETTER, {"heating": 2.4}, args={"error": 0.3, "baseline": 0.2})
        )
        assert failed.state == "learning"
        assert learnings.used(DRAW, "dev:hp1") is None


async def test_other_inputs_start_the_proof_over(tmp_path: Path) -> None:
    async with await Database.open(tmp_path / "t.db") as db:
        learnings = Learnings(db, AuditLog(tmp_path / "audit"))
        await learnings.load()
        await learnings.put(_draw("proved", PROVED, {"heating": 1.9}, fingerprint="meter-a"))
        found = await learnings.put(_draw("learning", FEW, {"heating": 2.0}, fingerprint="meter-b"))
        assert found.state == "learning"


def test_each_learners_too_little_to_judge_is_marked() -> None:
    for message in (
        FEW,
        house.FEW,
        heating.TESTING,
        hotwater.LEARNING,
        hotwater.LOSS_LEARNING,
        electric.FEW,
    ):
        assert message in UNJUDGED
    assert NOT_BETTER not in UNJUDGED
