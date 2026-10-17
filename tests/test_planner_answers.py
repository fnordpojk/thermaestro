"""The optimizer's answer as something is asked: ready by its time, how late or how warm
instead, a room short of its band, and what gives way for it."""

from dataclasses import replace
from datetime import UTC, datetime

from test_optimize_lp import full_slot_rise, hourly, slots, system, tank
from test_planner_assurance import CS, ROOM, TANK, room

from thermaestro.intents import kinds
from thermaestro.intents.entry import english
from thermaestro.optimize import Deadline, Problem, plan
from thermaestro.planner.answers import answer
from thermaestro.planner.optimizer import DeadlineGoal, Optimized, Watched

T = datetime(2026, 1, 12, tzinfo=UTC)


def bath_intent(id: str = "bath-1") -> object:
    asked = kinds.bath(TANK, 50.0, T, principal="user:a", created=T)
    return asked.model_copy(update={"id": id})


def planned(problem: Problem, watched: Watched) -> Optimized:
    return Optimized(True, "", problem, plan(problem), watched=watched)


def said(intent: object, preview: Optimized, current: Optimized | None = None) -> list[str]:
    others = {"bath-1": bath_intent()}
    return [english(s) for s in answer(intent, preview, current, others, UTC)]  # type: ignore[arg-type]


def hot_water(least: float, slot: int, estimated: bool = False) -> Optimized:
    t = tank(level=40.0, deadlines=(Deadline(slot=slot, at_least=least, rank=3, by="bath-1"),))
    problem = Problem(slots([0.5] * 16, estimated=estimated), tanks=(t,), slider=0.5)
    return planned(problem, Watched(deadlines=[DeadlineGoal("bath-1", TANK, slot, least, "must")]))


def test_ready_by_its_time() -> None:
    assert said(bath_intent(), hot_water(45.0, 7)) == ["The plan has it ready by 02:00"]


def test_late_and_how_warm_at_its_time_instead() -> None:
    rise = full_slot_rise(tank())
    least = 40.0 + 2.5 * rise  # two whole slots and a half: one slot more than there is
    [text] = said(bath_intent(), hot_water(least, 1))
    assert text.startswith("Ready about 15 minutes late, at 00:45; or ")
    assert text.endswith(" °C at 00:30")


def test_not_within_two_days() -> None:
    [text] = said(bath_intent(), hot_water(90.0, 7))  # above what a charge makes it
    assert text.startswith("It can't be made within two days: at most ")


def test_firms_up_once_tomorrows_prices_are_out() -> None:
    found = said(bath_intent(), hot_water(45.0, 7, estimated=True))
    assert found[-1] == "The plan firms up when tomorrow's prices are published"


def test_a_room_short_of_its_band() -> None:
    hours = 6
    cold = replace(system(hours, room=20.0), offset_high=10, draw_kw=0.6)
    problem = Problem(hourly([0.5] * hours, outdoor=-25.0), systems=(cold,), slider=0.5)
    warmer = kinds.warmer(CS, 1.0, principal="user:a", created=T).model_copy(
        update={"id": "band-1"}
    )
    [text] = said(warmer, planned(problem, Watched({ROOM: room(hours, low=20.5)})))
    assert text.startswith("Living room will reach ")
    assert ", not 20.5 °C, by " in text


def test_what_gives_way_for_it() -> None:
    """A cold house planned met without the new band; with it, the bath gives way."""
    hours = 2
    s = replace(system(hours, low=21.0, high=22.0, room=20.0), offset_low=-10, offset_high=10)
    t = tank(level=30.0, deadlines=(Deadline(slot=7, at_least=59.0, rank=3, by="bath-1"),))
    with_band = Problem(hourly([0.5] * hours, outdoor=-15.0), systems=(s,), tanks=(t,), slider=0.5)
    deadline = [DeadlineGoal("bath-1", TANK, 7, 59.0, "must")]
    preview = planned(with_band, Watched({ROOM: room(hours)}, deadline))
    alone = Problem(with_band.slots, tanks=(t,), slider=0.5)
    current = planned(alone, Watched(deadlines=deadline))
    band = kinds.comfort_band(CS, [("day", ((), None, None))], principal="user:a", created=T)
    found = said(band.model_copy(update={"id": "band-1"}), preview, current)
    assert "Bath gives way for it" in found
