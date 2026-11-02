"""A proof stands until a test with enough to judge by fails: too little since to judge a
model again keeps it proved; a failed test, or other inputs, doesn't."""

import json
from datetime import date
from pathlib import Path
from typing import Any, cast

import pytest

from thermaestro import clock
from thermaestro.core import AuditLog, Values
from thermaestro.core.values import Key
from thermaestro.learn import Learned, Learnings, electric, heating, hotwater, house, records
from thermaestro.learn.energy import DRAW as EDRAW
from thermaestro.learn.energy import Energy, charge_kw
from thermaestro.learn.heating import MODEL, RESPONSE, TAU, TESTING, Heating, SystemKeys
from thermaestro.learn.records import HouseDay, HouseHour, PowerRun
from thermaestro.learn.store import KEPT_PROOF, SET_ASIDE
from thermaestro.learn.validate import FEW, NOT_BETTER, PROVED, UNJUDGED
from thermaestro.store import Database, Home

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
        assert kept.value == {"heating": 2.0}  # the newest value learns on
        assert kept.proved == {"heating": 1.9}
        assert learnings.used(DRAW, "dev:hp1") == {"heating": 1.9}  # what was proved is used
        now[0] += 86_400  # still too little: when it was proved stays as it was
        again = await learnings.put(_draw("learning", FEW, None, args={"n": 3, "least": 24}))
        assert again.args == {"seen": proved_at}
        assert again.value == {"heating": 2.0}  # nothing new: the last one kept
        assert learnings.used(DRAW, "dev:hp1") == {"heating": 1.9}
        now[0] += 86_400  # judged with enough, and better: the newest is proved and used
        judged = await learnings.put(_draw("proved", PROVED, {"heating": 2.1}, args=args, n=30))
        assert (judged.proved, learnings.used(DRAW, "dev:hp1")) == (None, {"heating": 2.1})
        now[0] += 86_400  # judged, and not better: it isn't used any more
        failed = await learnings.put(
            _draw("learning", NOT_BETTER, {"heating": 2.4}, args={"error": 0.3, "baseline": 0.2})
        )
        assert failed.state == "learning"
        assert learnings.used(DRAW, "dev:hp1") is None


async def test_a_purpose_added_since_the_proof_isnt_used_until_proved(tmp_path: Path) -> None:
    """The compressor's draw proved for heating; hot-water runs seen since, with none older
    to test them against: heating's draw stays in use, hot water's isn't until a test with
    enough proves it. Kept so across a restart."""
    async with await Database.open(tmp_path / "t.db") as db:
        learnings = Learnings(db, AuditLog(tmp_path / "audit"))
        await learnings.load()
        energy = Energy(db, Values(db), cast(Any, None), learnings, lambda key: True, lambda: None)
        scope, now = "dev:hp1", 2_000_000_000.0

        async def runs(purpose: str, kw: float, start: float, count: int) -> None:
            rows = [
                records.row(records.POWER_RUN, start + i * 600, PowerRun(purpose=purpose, kw=kw))
                for i in range(count)
            ]
            await db.run(lambda t: records.keep(t, scope, rows))

        await runs("heating", 1.9, now - 4 * 86_400, 20)
        await runs("heating", 1.9, now - 86_400, 24)
        await energy._judge(scope, now)
        assert learnings.used(EDRAW, scope) == {"heating": 1.9}
        now += 4 * 86_400  # heating out of the test's two days; hot water seen, all new
        await runs("dhw", 3.2, now - 86_400, 10)
        await energy._judge(scope, now)
        found = learnings.get(EDRAW, scope)
        assert found is not None
        assert found.state == "proved"
        assert found.value == {"heating": 1.9, "dhw": 3.2}  # learned, not yet proved
        assert learnings.used(EDRAW, scope) == {"heating": 1.9}
        assert charge_kw(learnings) is None  # hot water: the planner's own guess
        restarted = Learnings(db, AuditLog(tmp_path / "audit"))
        await restarted.load()
        assert restarted.used(EDRAW, scope) == {"heating": 1.9}
        now += 3 * 86_400  # hot water seen before the test's days, and enough in them
        await runs("dhw", 3.2, now - 86_400, 24)
        await energy._judge(scope, now)
        assert charge_kw(learnings) == 3.2


async def test_a_proof_kept_the_earlier_way_is_set_aside_when_loaded(tmp_path: Path) -> None:
    """A row in the shape the earlier version wrote a kept proof in (no value proved beside
    it), found after the migration: judged again, not used."""
    async with await Database.open(tmp_path / "t.db") as db:
        body = {
            "name": DRAW,
            "scope": "dev:hp1",
            "kind": "model",
            "value": {"heating": 1.9, "dhw": 3.2},
            "state": "proved",
            "why": KEPT_PROOF,
            "args": {"seen": 1.0},
        }
        await db.run(
            lambda t: t.execute(
                "INSERT INTO learned (key, body, updated) VALUES (?, ?, ?)",
                (f"{DRAW}@dev:hp1", json.dumps(body), 1.0),
            )
        )
        learnings = Learnings(db, AuditLog(tmp_path / "audit"))
        await learnings.load()
        found = learnings.get(DRAW, "dev:hp1")
        assert found is not None
        assert (found.state, found.why) == ("learning", SET_ASIDE)
        assert learnings.used(DRAW, "dev:hp1") is None
        again = Learnings(db, AuditLog(tmp_path / "audit"))
        await again.load()  # written back: the same after a restart
        assert again.used(DRAW, "dev:hp1") is None


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


async def test_freezing_keeps_what_was_proved_in_use(tmp_path: Path) -> None:
    """The house's values proved, newer ones learned since and not yet judged: heating
    stopping freezes what was proved, not the newer ones, across a restart; heating back with
    too little to judge by keeps it; a test with enough puts the newest in use."""
    async with await Database.open(tmp_path / "t.db") as db:
        learnings = Learnings(db, AuditLog(tmp_path / "audit"))
        await learnings.load()
        scope, now = "dev:hp1/cs1", 2_000_000_000.0
        old_fit = house.Fit(0.2, 0.1, 0.01, 0.0, 0.0, 0.25, 4, 100, 0.1)
        new_fit = house.Fit(0.2, 0.1, 0.01, 0.0, 0.0, 0.125, 8, 110, 0.1)
        previous = {MODEL: old_fit.as_value(), TAU: 40.0, RESPONSE: 4.0}
        newer = {MODEL: new_fit.as_value(), TAU: 80.0, RESPONSE: 8.0}
        for name, value in previous.items():
            await learnings.put(
                Learned(
                    name=name, scope=scope, kind="model", value=value, state="proved", why=PROVED
                )
            )
            await learnings.put(
                Learned(
                    name=name,
                    scope=scope,
                    kind="model",
                    value=newer[name],
                    state="learning",
                    why=TESTING,
                )
            )
        stopped = HouseDay(
            day=date(2033, 5, 17),
            hours=[
                HouseHour(t=now - 3600, room=21.0, outdoor=20.0, water=25.0, skip="heating_off")
            ],
        )
        await db.run(
            lambda t: records.keep(t, scope, [records.row(records.HOUSE_DAY, now - 7200, stopped)])
        )
        point = Key("dev", "hp1/cs1/supply.temp")
        keys = SystemKeys(scope, point, point, [], point, point, [])
        learner = Heating(db, Values(db), cast(Any, None), learnings, lambda key: True)
        await learner._judge(keys, Home(), now, True)
        for name, value in previous.items():
            found = learnings.get(name, scope)
            assert found is not None
            assert found.state == "frozen"
            assert learnings.used(name, scope) == value
        restarted = Learnings(db, AuditLog(tmp_path / "audit"))
        await restarted.load()
        assert {name: restarted.used(name, scope) for name in previous} == previous
        back = await restarted.put(
            Learned(name=TAU, scope=scope, kind="model", value=90.0, state="learning", why=TESTING)
        )
        assert (back.state, restarted.used(TAU, scope)) == ("proved", 40.0)
        judged = await restarted.put(
            Learned(name=TAU, scope=scope, kind="model", value=90.0, state="proved", why=PROVED)
        )
        assert (judged.proved, restarted.used(TAU, scope)) == (None, 90.0)
