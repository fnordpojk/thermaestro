"""After a start: the planner waits for the plugins before it decides, judges or reports;
the watch's timers carry on; a failed solve changes no intent; and one failing step
doesn't end a round."""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from thermaestro import clock
from thermaestro.core import AuditLog, State, Values
from thermaestro.core.events import Events, Raise
from thermaestro.optimize import Problem, Slot
from thermaestro.planner import Planner, service
from thermaestro.planner import optimizer as optimizing
from thermaestro.planner.optimizer import Optimized, Optimizer
from thermaestro.planner.watch import DOWN, MISSING_S, Watch
from thermaestro.store import Database

NOW = datetime(2026, 10, 16, 0, 20, tzinfo=UTC)


class House:
    capabilities = None

    async def refresh(self) -> None:
        return None


def planner(db: Database, tmp_path: Path, host: Any, **kw: Any) -> Planner:
    nothing = cast(Any, None)
    return Planner(
        db, Values(db), host, nothing, nothing, cast(Any, House()), AuditLog(tmp_path / "a"), **kw
    )


async def test_a_round_waits_for_the_plugins(tmp_path: Path) -> None:
    starting = SimpleNamespace(state=State.STARTING, described=None)
    host = cast(Any, SimpleNamespace(instances={"pump": starting}))
    async with await Database.open(tmp_path / "t.db") as db:
        p = planner(db, tmp_path, host)
        plan = await p.round()
        assert plan.decisions == []
        assert p.situation is None  # nothing kept from an empty house
        starting.state = State.UP  # up, but not described yet
        assert not p._ready()
        starting.described = object()
        assert p._ready()
        starting.state = State.STARTING
        assert p._began is not None
        p._began = (
            clock.time() - service.READY_S - 1
        )  # waited long enough: plans with what there is
        assert p._ready()


async def test_an_open_plugin_missing_event_stays_open_after_a_restart(tmp_path: Path) -> None:
    down = SimpleNamespace(state=State.RESTARTING, health=None, active={})
    host = cast(Any, SimpleNamespace(instances={"pump": down}))
    async with await Database.open(tmp_path / "t.db") as db:
        events = Events(db)
        now = NOW.timestamp()
        missing = Raise("plugin.missing", "pump", DOWN, {"name": "pump", "state_label": "x"})
        await events.raise_("plugin.missing:pump", missing, now=now - 2 * MISSING_S)
        watch = Watch(events, cast(Any, SimpleNamespace(claims={})), host)
        await watch.resume()
        assert "plugin.missing:pump" in watch._plugins(now)  # still missing: still told


async def test_the_watchs_timers_as_kept(tmp_path: Path) -> None:
    async with await Database.open(tmp_path / "t.db") as db:
        watch = Watch(Events(db), cast(Any, None), cast(Any, None))
        state = {"since": {"k": 1.0}, "low": {"room:a": 20.5}, "rose": {"room:a": 2.0}, "below": {}}
        watch.load(state, fresh=False)
        assert (watch._rose, watch._low, watch._since) == ({"room:a": 2.0}, {"room:a": 20.5}, {})
        watch.load(state, fresh=True)
        assert watch._since == {"k": 1.0}
        assert watch.state()["rose"] == {"room:a": 2.0}


async def test_a_failed_solve_changes_no_intent(tmp_path: Path) -> None:
    reported: list[str] = []

    class Intents:
        async def all(self) -> list[Any]:
            return [SimpleNamespace(id="in-1", state="at_risk", scope="x", why_args={})]

        async def report(self, id: str, *args: Any, **kw: Any) -> None:
            reported.append(id)

    async with await Database.open(tmp_path / "t.db") as db:
        nothing = cast(Any, None)
        p = Planner(
            db,
            Values(db),
            nothing,
            nothing,
            cast(Any, Intents()),
            cast(Any, House()),
            AuditLog(tmp_path / "a"),
        )
        await p._assure(Optimized(False, "the solve failed"), cast(Any, None))
        assert reported == []  # at risk stays at risk: no plan said otherwise


async def test_a_failing_solve_is_a_failed_plan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(problem: Problem, limit: float) -> Any:
        raise RuntimeError("the solver crashed")

    monkeypatch.setattr(optimizing, "solve", broken)
    async with await Database.open(tmp_path / "t.db") as db:
        opt = Optimizer(db, Values(db), cast(Any, None), cast(Any, None))
        problem = Problem((Slot(NOW.timestamp(), price=0.5),))

        async def made(sit: Any, resolver: Any, **kw: Any) -> Any:
            return problem, frozenset({"pump:hp1/cs1/heating.offset"}), []

        opt.problem = made  # type: ignore[method-assign]
        found = await opt.run(cast(Any, SimpleNamespace(now=NOW + timedelta(0))), cast(Any, None))
        assert not found.ok
        assert "the solver crashed" in found.why
