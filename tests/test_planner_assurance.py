"""How each intent stands in the optimizer's plan: at risk, giving way, or planned met."""

from dataclasses import replace
from datetime import UTC

from test_optimize_lp import hourly, system, tank

from thermaestro.optimize import Deadline, Problem, plan
from thermaestro.planner.assurance import judge
from thermaestro.planner.optimizer import DeadlineGoal, RoomGoal, Watched

TANK = "pump:hp1/dhw"
ROOM = "room:living"
CS = "pump:hp1/cs1"


def room(hours: int, low: float = 21.0, reach_h: float = 1.0) -> RoomGoal:
    return RoomGoal(CS, "Living room", (low,) * hours, (("band-1",),) * hours, reach_h)


def bath(least: float = 59.0) -> tuple[Watched, Problem]:
    hours = 2
    s = replace(system(hours, low=21.0, high=22.0, room=20.0), offset_low=-10, offset_high=10)
    t = tank(level=30.0, deadlines=(Deadline(slot=7, at_least=least, rank=3, by="bath-1"),))
    problem = Problem(hourly([0.5] * hours, outdoor=-15.0), systems=(s,), tanks=(t,), slider=0.5)
    watched = Watched({ROOM: room(hours)}, [DeadlineGoal("bath-1", TANK, 7, least, "must")])
    return watched, problem


def judged(watched: Watched, problem: Problem) -> dict[str, tuple[str, str]]:
    found = judge(watched, problem, plan(problem), UTC, plan)
    return {k: (f.state, f.text()) for k, f in found.items()}


def test_a_bath_short_for_the_rooms_gives_way_to_comfort() -> None:
    watched, problem = bath()
    found = judged(watched, problem)
    state, text = found["bath-1"]
    assert state == "giving_way"
    assert text.startswith("Gives way to comfort: the plan has the hot water at ")
    assert text.endswith(", short of 59.0 °C")


def test_a_bath_no_charge_can_make_is_at_risk() -> None:
    watched, problem = bath(least=80.0)  # above what the compressor can make the tank
    state, text = judged(watched, problem)["bath-1"]
    assert state == "at_risk"
    assert "short of 80.0 °C" in text


def test_a_room_the_compressor_cant_keep_warm_is_at_risk_once_it_could_be_reached() -> None:
    hours = 6
    cold = replace(system(hours, room=20.0), offset_high=10, draw_kw=0.6)
    problem = Problem(hourly([0.5] * hours, outdoor=-25.0), systems=(cold,), slider=0.5)
    found = judged(Watched({ROOM: room(hours, low=20.5)}), problem)
    state, text = found["band-1"]
    assert state == "at_risk"
    assert text.startswith("The plan has Living room at ")
    assert text.endswith("below its 20.5 °C")
    # Not before its heat could have shown.
    assert judged(Watched({ROOM: room(hours, low=20.5, reach_h=12)}), problem) == {}


def test_what_the_plan_meets_isnt_reported() -> None:
    watched, problem = bath(least=40.0)
    assert "bath-1" not in judged(watched, problem)
