"""The optimizer's plan: checked against an exhaustive search on small cases, against the
house model stepped on its own, and for what must never give way to price."""

import itertools
from collections.abc import Sequence
from dataclasses import replace
from typing import Any

import pytest

from thermaestro.learn.cop import carnot
from thermaestro.learn.house import Fit, Hour
from thermaestro.learn.pump import Water
from thermaestro.optimize import (
    Addition,
    Band,
    Deadline,
    Pool,
    Problem,
    Room,
    Slot,
    System,
    Tank,
    plan,
)
from thermaestro.optimize.problem import SLOT_H

T0 = 1_768_176_000.0  # 2026-01-12, midnight UTC
FIT = Fit(alpha=0.3, beta=0.1, sigma=0.0, omega=0.0, gamma=0.0, r=0.2, lag=1, rows=0, error=0.0)
WATER = Water(w0=28.0, w1=-0.5, w2=2.0, lift=2.0, rows=0)


def slots(prices: Sequence[float | None], outdoor: float = 0.0, **kw: Any) -> tuple[Slot, ...]:
    return tuple(
        Slot(T0 + 900 * j, price=p, outdoor=outdoor, source=5.0, **kw) for j, p in enumerate(prices)
    )


def hourly(prices: list[float], **kw: Any) -> tuple[Slot, ...]:
    return slots([p for p in prices for _ in range(4)], **kw)


def system(hours: int, low: float = 20.5, high: float = 22.0, room: float = 21.0) -> System:
    return System(
        scope="pump:hp1/cs1",
        fit=FIT,
        water=WATER,
        emitters_kw_k=0.3,
        offset_low=-10,
        offset_high=10,
        offset_now=0,
        room=room,
        water_before=(28.0,),
        rooms=(Room("room:living", 0.0, tuple(Band(low, high) for _ in range(hours))),),
        share=0.5,
        draw_kw=1.5,
    )


# --- a tank, against every way of charging it ----------------------------------------------


def tank(**kw: object) -> Tank:
    base = Tank(
        scope="pump:hp1/dhw",
        level=46.0,
        kwh_per_k=0.21,
        loss=0.0,
        draws=(0.0,) * 8,
        ceiling=60.0,
        share=0.47,
        draw_kw=1.6,
    )
    return replace(base, **kw)  # type: ignore[arg-type]


def full_slot_rise(t: Tank, source: float = 5.0) -> float:
    """What a whole slot of charging adds, worked out on its own."""
    cop = t.share * (t.ceiling + 273.15) / (t.ceiling - source)
    return t.draw_kw * cop * 0.25 / t.kwh_per_k


def test_a_charge_goes_where_every_way_of_charging_says_is_cheapest() -> None:
    prices = [0.9, 0.4, 0.7, 0.3, 0.8, 0.35, 0.6, 1.2]
    t = tank()
    need = t.level + full_slot_rise(t) * 2  # two whole slots' worth
    t = replace(t, deadlines=(Deadline(slot=6, at_least=need, rank=3),))
    p = Problem(slots(prices), tanks=(t,), slider=1.0)
    found = plan(p)
    assert found.ok, found.why
    assert found.stages["must_deadlines"] == pytest.approx(0.0, abs=2e-3)
    best = None
    for charges in itertools.product((0, 1), repeat=8):
        level = t.level
        for j, on in enumerate(charges):
            level += on * full_slot_rise(t)
            if j == 6 and level < need - 1e-9:
                break
        else:
            cost = sum(on * t.draw_kw * 0.25 * prices[j] for j, on in enumerate(charges))
            best = cost if best is None else min(best, cost)
    assert best is not None
    assert found.cost == pytest.approx(best, abs=1e-3)
    shares = found.tanks[t.scope].share
    assert shares[3] == pytest.approx(1.0, abs=1e-3)
    assert shares[5] == pytest.approx(1.0, abs=1e-3)
    assert shares[7] == pytest.approx(0.0)  # after the deadline nothing is needed


def test_the_tanks_level_is_its_own_physics() -> None:
    t = tank(loss=0.02, draws=(0.0, 0.0, 3.0, 0.0, 0.0, 0.0, 0.0, 0.0))
    t = replace(t, deadlines=(Deadline(slot=7, at_least=50.0, rank=3),))
    found = plan(Problem(slots([0.5] * 8), tanks=(t,), slider=0.5))
    assert found.ok
    level = t.level
    for j, share in enumerate(found.tanks[t.scope].share):
        level += -t.loss * (level - t.room_c) * SLOT_H - t.draws[j]
        level += share * t.draw_kw * t.share * carnot(t.ceiling, 5.0) * SLOT_H / t.kwh_per_k
        assert found.tanks[t.scope].level[j] == pytest.approx(level, abs=1e-6)


def test_the_floor_holds_whatever_the_price() -> None:
    t = tank(level=44.0, loss=0.05, floor=42.0)
    found = plan(Problem(slots([50.0] * 16), tanks=(t,), slider=1.0))
    assert found.ok
    assert found.stages["floors"] == pytest.approx(0.0, abs=2e-3)
    assert min(found.tanks[t.scope].level) >= 42.0 - 5e-3  # within the stage's tolerance


# --- the house ------------------------------------------------------------------------------


def test_the_rooms_follow_the_house_model_stepped_on_its_own() -> None:
    hours = 12
    p = Problem(hourly([0.5] * 6 + [1.5] * 6), systems=(system(hours),), slider=0.8)
    found = plan(p)
    assert found.ok, found.why
    planned = found.systems["pump:hp1/cs1"]
    s = p.systems[0]
    mass = FIT.mass(s.room, Hour(T0, s.room, 0.0, None), s.water_before[-1])
    assert mass is not None
    room = s.room
    for h, offset in enumerate(planned.offset):
        mass += FIT.r * (room - mass)
        room = FIT.room(mass, Hour(T0, None, 0.0, None), WATER.water(0.0, offset))
        assert planned.room[h] == pytest.approx(room, abs=1e-6)
        # The compressor and the addition give the heat the water hands the house (these
        # radiators store none), within the last stage's rounding.
        heat = 0.3 * (WATER.water(0.0, offset) - room)
        assert planned.heat_kw[h] == pytest.approx(heat, abs=2e-3)


def test_cheap_hours_store_heat_in_the_house_and_dear_ones_use_it() -> None:
    hours = 12
    p = Problem(hourly([0.2] * 6 + [2.0] * 6), systems=(system(hours),), slider=1.0)
    found = plan(p)
    assert found.ok
    planned = found.systems["pump:hp1/cs1"]
    assert max(planned.room[:6]) > 21.25  # warmed while cheap
    assert sum(planned.offset[:6]) > sum(planned.offset[6:])
    assert found.stages["comfort_low"] == pytest.approx(0.0, abs=2e-3)
    assert found.stages["comfort_high"] == pytest.approx(0.0, abs=2e-3)
    # Without a cost stance nothing is moved by price: the rooms kept near the middle.
    still = plan(replace(p, slider=None))
    assert max(abs(r - 21.25) for r in still.systems["pump:hp1/cs1"].room[2:]) < 0.15


def test_extreme_prices_buy_neither_cold_nor_heat() -> None:
    hours = 8
    expensive = plan(Problem(hourly([1000.0] * hours), systems=(system(hours),), slider=1.0))
    assert expensive.ok
    assert expensive.stages["comfort_low"] == pytest.approx(0.0, abs=2e-3)
    assert min(expensive.systems["pump:hp1/cs1"].room) >= 20.5 - 2e-3
    free = plan(Problem(hourly([-5.0] * hours), systems=(system(hours),), slider=1.0))
    assert free.ok
    assert free.stages["comfort_high"] == pytest.approx(0.0, abs=2e-3)
    assert max(free.systems["pump:hp1/cs1"].room) <= 22.0 + 2e-3


def test_the_ranking_decides_what_gives_way() -> None:
    """A cold house and a bath, with one compressor that can't do both at once: comfort's
    low edge first by default; the bath first once the household ranks it higher."""
    hours = 2
    s = replace(system(hours, low=21.0, high=22.0, room=20.0), offset_low=-10, offset_high=10)
    t = tank(level=30.0, deadlines=(Deadline(slot=7, at_least=59.0, rank=3),))
    p = Problem(hourly([0.5] * hours, outdoor=-15.0), systems=(s,), tanks=(t,), slider=0.5)
    first = plan(p)
    assert first.ok, first.why
    assert first.stages["must_deadlines"] > 0.5  # the bath falls short
    swapped = plan(
        replace(
            p,
            ranking=(
                "must_deadlines",
                "comfort_low",
                "should_deadlines",
                "comfort_high",
                "power_peak",
            ),
            tanks=(replace(t, deadlines=(Deadline(slot=7, at_least=59.0, rank=2),)),),
        )
    )
    assert swapped.stages["must_deadlines"] == pytest.approx(0.0, abs=2e-3)
    assert swapped.stages["comfort_low"] > first.stages["comfort_low"]


def test_the_addition_follows_its_policy() -> None:
    hours = 4
    cold = replace(system(hours, room=20.0), offset_high=10)
    p = Problem(
        hourly([0.5] * hours, outdoor=-25.0),
        systems=(cold,),
        addition=Addition(kw=6.0, policy="pump"),
        slider=0.5,
    )
    found = plan(p)
    assert found.ok
    never = plan(replace(p, addition=Addition(kw=6.0, policy="limit", limit_kw=0.0)))
    assert never.ok
    assert max(never.systems["pump:hp1/cs1"].addition_kw) == pytest.approx(0.0, abs=1e-9)


def test_the_pumps_own_addition_gives_what_the_compressor_cant() -> None:
    """Not planned or steered: counted where the compressor falls short, at most half its
    power, and not while the pump's mean outdoor temperature is at its stop or above."""
    hours = 6
    small = replace(system(hours, room=20.0), offset_high=10, draw_kw=0.6)
    p = Problem(hourly([0.5] * hours, outdoor=-25.0), systems=(small,), slider=0.5)
    without = plan(p)
    own = plan(replace(p, addition=Addition(kw=6.0, own=True, stop_c=5.0, mean_c=-20.0)))
    assert without.ok
    assert own.ok
    assert without.stages["comfort_low"] > 0.1  # the compressor alone can't keep it warm
    assert own.stages["comfort_low"] < without.stages["comfort_low"]
    used = own.systems["pump:hp1/cs1"].addition_kw
    assert 0 < max(used) <= 3.0 + 1e-6
    # Its kW are the house's: in what the plan draws, and paid at a COP of 1.
    assert all(own.kw[4 * h] >= kw - 1e-6 for h, kw in enumerate(used))
    # Off at its stop.
    stopped = plan(replace(p, addition=Addition(kw=6.0, own=True, stop_c=-30.0, mean_c=-25.0)))
    assert max(stopped.systems["pump:hp1/cs1"].addition_kw) == pytest.approx(0.0, abs=1e-9)
    assert stopped.stages["comfort_low"] == pytest.approx(without.stages["comfort_low"], abs=2e-3)
    mild = plan(replace(p, addition=Addition(kw=6.0, own=True, stop_c=-10.0, mean_c=8.0)))
    # Every hour is colder than its stop, but the pump's mean of the day isn't yet.
    assert max(mild.systems["pump:hp1/cs1"].addition_kw) == pytest.approx(0.0, abs=1e-9)


def test_the_power_limit_gives_way_only_to_what_ranks_above_it() -> None:
    t = tank(level=45.0, deadlines=(Deadline(slot=3, at_least=50.0, rank=4),))
    limited = slots([0.5] * 16, power_limit=1.2, other_kw=1.0)
    found = plan(Problem(limited, tanks=(t,), slider=0.5))
    assert found.ok
    # 0.2 kW to spare, as a slot's mean, isn't enough to make it within the hour, and a
    # should deadline ranks above the power limit: it charges anyway, and says by how
    # much it went over.
    assert found.stages["should_deadlines"] == pytest.approx(0.0, abs=2e-3)
    assert found.stages["power_peak"] > 0


def test_a_pool_is_kept_in_its_band_cheaply() -> None:
    pool = Pool(
        scope="pump:hp1/pool1",
        temp=24.0,
        a=0.2,
        b=0.002,
        bands=tuple(Band(24.0, 28.0) for _ in range(16)),
        draw_kw=1.5,
    )
    prices = [1.0] * 8 + [0.1] * 4 + [1.0] * 4
    found = plan(Problem(slots(prices, outdoor=5.0), pools=(pool,), slider=1.0))
    assert found.ok
    assert found.stages["pools"] == pytest.approx(0.0, abs=2e-3)
    shares = found.pools[pool.scope].share
    assert sum(shares[8:12]) > sum(shares[:8])  # heated in the cheap hour
