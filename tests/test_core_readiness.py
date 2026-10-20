"""Whether Thermaestro can control now: what `/ready` answers."""

from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

from thermaestro.cap.model import Envelope
from thermaestro.core import State, Values
from thermaestro.core.readiness import INPUT_S, ROUND_S, readiness
from thermaestro.store import Database

NOW = 2_000_000_000.0


def executor(**kw: Any) -> Any:
    state = {"planner": "checking in", "not_put_back": 0, "in_flight": 0, "tasks_down": 0}
    return SimpleNamespace(readiness=lambda: state | kw)


def planner(age: float | None = 60.0, running: bool = True) -> Any:
    return SimpleNamespace(readiness=lambda now: {"round_age_s": age, "running": running})


def pump(state: State = State.UP) -> Any:
    return SimpleNamespace(state=state, described=SimpleNamespace(levers=("hp1/offset",)))


def reading(values: Values, ago: float) -> None:
    from datetime import UTC, datetime

    at = datetime.fromtimestamp(NOW - ago, UTC)
    values.add(
        "pump",
        Envelope(
            point="hp1/x",
            value=1.0,
            t_observed=at,
            t_received=at,
            quality="good",
            source="measured",
        ),
    )


async def test_ready_only_with_everything_running(tmp_path: Path) -> None:
    async with await Database.open(tmp_path / "t.db") as db:
        values = Values(db)
        values.flush_s, values.flushed = 60.0, NOW - 10
        reading(values, 30)
        host = cast(Any, SimpleNamespace(instances={"pump": pump()}))

        def ask(**kw: Any) -> dict[str, Any]:
            parts: dict[str, Any] = {
                "executor": executor(),
                "planner": planner(),
                "values": values,
                "host": host,
            }
            return readiness(**(parts | kw), now=NOW)

        assert ask() == {
            "ready": True,
            "planner": "checking in",
            "round_age_s": 60,
            "devices_down": 0,
            "inputs_stale": 0,
            "history": "writing",
            "tasks_down": 0,
            "not_put_back": 0,
            "in_flight": 0,
        }
        assert ask(planner=planner(age=ROUND_S + 1))["ready"] is False  # no whole round
        assert ask(planner=planner(age=None))["ready"] is False
        assert ask(planner=planner(running=False))["tasks_down"] == 1
        assert ask(executor=executor(tasks_down=1))["ready"] is False  # a loop of the write path
        assert ask(executor=executor(not_put_back=1))["ready"] is False
        down = cast(Any, SimpleNamespace(instances={"pump": pump(State.RESTARTING)}))
        assert ask(host=down)["devices_down"] == 1
        values.flushed = NOW - 181  # three of its periods and more
        assert ask()["history"] == "stalled"
        values.flushed = NOW - 10
        stale = Values(db)
        stale.flush_s, stale.flushed = 60.0, NOW
        reading(stale, INPUT_S + 1)
        assert ask(values=stale)["inputs_stale"] == 1
        assert ask(values=stale)["ready"] is False


async def test_the_history_writer_says_whether_it_runs(tmp_path: Path) -> None:
    async with await Database.open(tmp_path / "t.db") as db:
        values = Values(db)
        assert values.writing(NOW) == "not running"
        values.flush_s, values.flushed = 60.0, NOW - 60
        assert values.writing(NOW) == "writing"
        assert values.writing(NOW + 200) == "stalled"
