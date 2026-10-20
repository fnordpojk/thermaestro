"""From the plan to the levers: the offset's steps within the write budget, a tank's
charges and holds, compressor starts at the pump's own pace, the addition and a pool."""

from dataclasses import replace
from datetime import UTC, datetime

import pytest

from thermaestro.optimize import Addition, Band, Deadline, Plan, Pool, Problem, Slot, Tank
from thermaestro.optimize.levers import Now, TankNow, decide, steps
from thermaestro.optimize.problem import PoolPlan, SystemPlan, TankPlan
from thermaestro.planner.model import LeverState

T0 = datetime(2026, 1, 12, 10, 0, tzinfo=UTC)
OFFSET = LeverState(
    "pump:hp1/cs1/heating.offset", "setting", low=-10, high=10, step=1, current=0, baseline=0
)
TANK = "pump:hp1/dhw"
BLOCK = LeverState(f"{TANK}/block", "hold")
BOOST = LeverState(f"{TANK}/boost_once", "trigger")


def test_the_offset_follows_the_plan_in_whole_steps() -> None:
    targets = [0.2] * 4 + [1.8] * 4 + [2.6] * 8
    found = steps(targets, 0, OFFSET, 2.0, 16)
    assert found[:4] == [0] * 4
    assert found[-1] == 2
    # Up a step at a time: a jump of two would run the pump's degree minutes deep.
    assert all(b - a <= 1 for a, b in zip([0, *found], found, strict=False))
    # Half a step off is on it: the plan's numbers wandering between two steps don't move it.
    assert steps([0.4, 0.6, 0.45, 0.55] * 4, 0, OFFSET, 2.0, 16) == [0] * 16


def test_with_little_budget_left_the_offset_changes_less() -> None:
    wandering = [0.0] * 6 + [3.0] * 10
    free = steps(wandering, 0, OFFSET, 2.0, 16)
    tight = steps(wandering, 0, OFFSET, 2.0, 1)
    changes = sum(1 for a, b in zip([0, *tight], tight, strict=False) if a != b)
    assert changes <= 1
    assert sum(1 for a, b in zip([0, *free], free, strict=False) if a != b) > changes


def tank(**kw: object) -> Tank:
    base = Tank(TANK, 47.0, 0.21, 0.01, (0.0,) * 16, 55.0, 0.47, 1.6, floor=40.0)
    return replace(base, **kw)  # type: ignore[arg-type]


def problem(t: Tank) -> Problem:
    slots = tuple(Slot(T0.timestamp() + 900 * j, price=0.5) for j in range(16))
    return Problem(slots, tanks=(t,), slider=0.5)


def planned(share: list[float]) -> Plan:
    return Plan(True, tanks={TANK: TankPlan(TANK, share, [50.0] * len(share))})


def now(**kw: object) -> Now:
    base = Now(
        T0,
        UTC,
        {BLOCK.ref: BLOCK, BOOST.ref: BOOST},
        tanks={TANK: TankNow(48.0, 40.0, False)},
        shifting=True,
        compressor_on=True,
    )
    return replace(base, **kw)  # type: ignore[arg-type]


def test_a_charge_the_plan_makes_now_is_started() -> None:
    t = tank(deadlines=(Deadline(slot=7, at_least=52.0, rank=3),))
    [d] = decide(planned([1.0] + [0.0] * 15), problem(t), now())
    assert (d.lever, d.op, d.rank) == (BOOST.ref, "fire", 3)
    assert d.reason.startswith("hot water 52 °C by 12:00: the plan charges now")


def test_the_pumps_own_charges_are_held_off_until_the_plan_charges() -> None:
    [d] = decide(planned([0.0] * 8 + [1.0] + [0.0] * 7), problem(tank()), now())
    assert (d.lever, d.op) == (BLOCK.ref, "engage")
    assert d.reason == "the plan: the pump's own charges held off; it charges at 12:00"
    held = now(levers={BLOCK.ref: replace(BLOCK, held=True), BOOST.ref: BOOST})
    # For price alone, it is let go and the pump charges by itself: no charge is started.
    found = decide(planned([0.6] + [0.0] * 15), problem(tank()), held)
    assert [(d.lever, d.op) for d in found] == [(BLOCK.ref, "release")]
    # Held until the plan charges within the slot after; once let go, held again only
    # while nothing is planned for an hour.
    assert decide(planned([0.0] * 3 + [1.0] + [0.0] * 12), problem(tank()), held) == []
    assert decide(planned([0.0] * 3 + [1.0] + [0.0] * 12), problem(tank()), now()) == []
    # Without a cost stance or prices, the pump's own thermostat stands.
    assert decide(planned([0.0] * 16), problem(tank()), now(shifting=False)) == []


def test_a_block_held_the_longest_is_let_go_and_not_held_again_until_a_charge() -> None:
    spent = {TANK: TankNow(48.0, 40.0, False, spent=True)}
    held = now(levers={BLOCK.ref: replace(BLOCK, held=True), BOOST.ref: BOOST}, tanks=spent)
    # The plan would hold on: nothing is planned for hours.
    found = decide(planned([0.0] * 16), problem(tank()), held)
    assert [(d.lever, d.op, d.reason) for d in found] == [
        (BLOCK.ref, "release", "held long enough: the pump may charge again")
    ]
    assert decide(planned([0.0] * 16), problem(tank()), now(tanks=spent)) == []


def test_near_its_floor_every_hold_goes_whatever_the_plan() -> None:
    held = now(
        levers={BLOCK.ref: replace(BLOCK, held=True), BOOST.ref: BOOST},
        tanks={TANK: TankNow(39.5, 40.0, False)},
    )
    found = decide(planned([0.0] * 16), problem(tank()), held)
    assert [(d.op, d.rank) for d in found] == [("release", 1), ("fire", 1)]


def test_a_boost_asked_for_now_is_fired_whatever_the_plan() -> None:
    """The plan charges only for deadlines; a boost now, asked by the household, is carried
    out at once, a hold on the pump's charges let go for it."""
    held = now(
        levers={BLOCK.ref: replace(BLOCK, held=True), BOOST.ref: BOOST},
        boosts={TANK: 2},
    )
    found = decide(planned([0.0] * 16), problem(tank()), held)
    assert [(d.lever, d.op, d.rank, d.reason) for d in found] == [
        (BLOCK.ref, "release", 2, "boost now, as asked"),
        (BOOST.ref, "fire", 2, "boost now, as asked"),
    ]
    # Already charging: nothing more to ask, and nothing held off for price meanwhile.
    charging = now(boosts={TANK: 2}, tanks={TANK: TankNow(48.0, 40.0, True)})
    assert decide(planned([0.0] * 16), problem(tank()), charging) == []


SHOULD = (Deadline(slot=7, at_least=52.0, rank=4),)


def test_starts_stay_at_the_pumps_own_pace() -> None:
    standing = now(compressor_on=False, stopped_s=3600.0, starts_today=20, pace=24.0)
    # By 10:00 the pump on its own would have started ten times, not twenty: a charge for
    # a deadline that should be met, not must, waits for the compressor to run.
    found = decide(planned([1.0] + [0.0] * 15), problem(tank(deadlines=SHOULD)), standing)
    assert [d.op for d in found] == []
    # A must deadline doesn't wait.
    urgent = tank(deadlines=(Deadline(slot=3, at_least=52.0, rank=3),))
    assert [d.op for d in decide(planned([1.0] + [0.0] * 15), problem(urgent), standing)] == [
        "fire"
    ]
    # Nor does a compressor already running: no start to count.
    running = replace(standing, compressor_on=True, stopped_s=None)
    should = problem(tank(deadlines=SHOULD))
    assert [d.op for d in decide(planned([1.0] + [0.0] * 15), should, running)] == ["fire"]


def test_a_compressor_that_just_stopped_isnt_started_for_price() -> None:
    just = now(compressor_on=False, stopped_s=300.0)
    assert decide(planned([1.0] + [0.0] * 15), problem(tank(deadlines=SHOULD)), just) == []


def test_the_addition_is_kept_off_where_the_plan_needs_none() -> None:
    stop = LeverState(
        "pump:hp1/addition/stop_temp", "setting", low=-30, high=10, step=1, current=5, baseline=5
    )
    p = Problem(
        (Slot(T0.timestamp()),) * 4, addition=Addition(kw=6.0, policy="when_needed"), slider=0.5
    )
    plan = Plan(True, systems={"pump:hp1/cs1": SystemPlan("pump:hp1/cs1", addition_kw=[0.0])})
    [d] = decide(plan, p, now(levers={stop.ref: stop}))
    # At -15 °C, not the lever's lowest: left there, it still helps in real cold.
    assert (d.op, d.params) == ("set", {"value": -15})
    assert decide(plan, p, now(levers={stop.ref: replace(stop, current=-15)})) == []
    used = Plan(True, systems={"pump:hp1/cs1": SystemPlan("pump:hp1/cs1", addition_kw=[2.0])})
    off = replace(stop, current=-15)
    [d] = decide(used, p, now(levers={stop.ref: off}))
    assert d.op == "restore"
    # Left to the pump, as the household chose.
    pump = replace(p, addition=Addition(kw=6.0, policy="pump"))
    assert decide(plan, pump, now(levers={stop.ref: stop})) == []


def test_a_pool_heats_when_the_plan_heats_it() -> None:
    scope = "pump:hp1/pool1"
    start = LeverState(f"{scope}/start_temp", "setting", low=5, high=35, step=0.5, current=22)
    pool = Pool(scope, 25.0, 0.2, 0.002, (Band(24.0, 28.0),) * 16, 1.5)
    p = Problem(tuple(Slot(T0.timestamp() + 900 * j) for j in range(16)), pools=(pool,))
    heats = Plan(True, pools={scope: PoolPlan(scope, [1.0] + [0.0] * 15, [25.0] * 16)})
    [d] = decide(heats, p, now(levers={start.ref: start}, pools={scope: 25.0}))
    assert d.params == {"value": pytest.approx(25.5)}
    rests = Plan(True, pools={scope: PoolPlan(scope, [0.0] * 16, [25.0] * 16)})
    [d] = decide(rests, p, now(levers={start.ref: start}, pools={scope: 25.0}))
    assert d.params == {"value": 24.0}


def test_a_room_outside_its_band_is_never_stepped_away_from() -> None:
    from thermaestro.learn.house import Fit
    from thermaestro.learn.pump import Water
    from thermaestro.optimize import Room, System
    from thermaestro.optimize.problem import SystemPlan

    fit = Fit(0.3, 0.1, 0.0, 0.0, 0.0, 0.2, 1, 0, 0.1)
    system = System(
        "pump:hp1/cs1",
        fit,
        Water(28.0, -0.5, 2.0, 2.0, 0),
        0.3,
        -10,
        10,
        0,
        20.0,
        (28.0,),
        (Room("room:r1", 0.0, (Band(20.5, 22.0),) * 4),),
        0.5,
        1.5,
    )
    p = Problem(tuple(Slot(T0.timestamp() + 900 * j) for j in range(16)), systems=(system,))
    lower = Plan(
        True, systems={system.scope: SystemPlan(system.scope, offset=[-2.0] * 4, room=[21.0] * 4)}
    )
    cold = now(levers={OFFSET.ref: OFFSET}, rooms_below={system.scope: "Bedroom"})
    assert decide(lower, p, cold) == []
    warm = now(levers={OFFSET.ref: OFFSET}, rooms_above={system.scope: "Bedroom"})
    higher = Plan(
        True, systems={system.scope: SystemPlan(system.scope, offset=[2.0] * 4, room=[21.0] * 4)}
    )
    assert decide(higher, p, warm) == []
    assert [d.params for d in decide(lower, p, warm)] == [{"value": -2}]  # lowered at once
