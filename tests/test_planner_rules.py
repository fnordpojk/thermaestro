"""The planner's rules, one situation at a time: what each asks of the levers, and why."""

from dataclasses import replace
from datetime import UTC, datetime, time, timedelta

from thermaestro.intents import RANKS, Calendar, Capabilities, Intent, Level, Resolver, kinds
from thermaestro.planner import (
    Decision,
    GridLimit,
    LeverState,
    Memory,
    Price,
    RoomReading,
    Situation,
    Tank,
    rules,
)
from thermaestro.planner.model import RANK_SLIDER, SLOT

NOW = datetime(2026, 1, 15, 12, 0, tzinfo=UTC)
BEFORE = NOW - timedelta(days=2)
CS = "pump:hp1/cs1"
ROOM = "room:living"
TANK = "pump:hp1/dhw"
OFFSET = f"{CS}/heating.offset"
BLOCK = f"{TANK}/block"
BOOST = f"{TANK}/boost_once"
LEVELS = {
    "day": Level(id="day", name="Day", scope=CS, low=20.5, high=22.0),
    "wide": Level(id="wide", name="Wide", scope=CS, low=20.0, high=23.0),
    "tank": Level(id="tank", name="Tank", scope=TANK, top=50.0),
}
CAPS = Capabilities(
    systems=frozenset({CS}),
    rooms={ROOM: CS},
    offset=frozenset({CS}),
    tanks=frozenset({TANK}),
    block=frozenset({TANK}),
    prices=True,
)


def who(**kw: object) -> dict[str, object]:
    return {"principal": "user:a", "created": BEFORE, **kw}


def band(level: str = "day") -> Intent:
    return kinds.comfort_band(CS, [(level, ((), None, None))], principal="user:a", created=BEFORE)


def stance(slider: float = 1.0) -> Intent:
    return kinds.cost_stance(ranking=RANKS, slider=slider, principal="user:a", created=BEFORE)


def floor(temp: float = 40.0) -> Intent:
    return kinds.hot_water_floor(TANK, temp, principal="user:a", created=BEFORE)


def prices(cheap_now: bool | None = None) -> list[Price]:
    """A day of prices around now: expensive mornings and evenings, cheap nights and early
    afternoons; now itself (noon) cheap, expensive or ordinary as asked."""
    out = []
    t = NOW - timedelta(hours=12)
    while t < NOW + timedelta(hours=24):
        hour = t.hour
        value = 1.0 + (0.8 if hour in (7, 8, 17, 18, 19) else 0.0) - (0.5 if hour < 5 else 0.0)
        value -= 0.4 if 13 <= hour < 16 else 0.0
        if t == NOW and cheap_now is not None:
            value = 0.2 if cheap_now else 2.5
        out.append(Price(t, value))
        t += SLOT
    return out


def situation(
    intents: list[Intent],
    *,
    temp: float | None = 21.25,
    age_s: float = 60.0,
    price: list[Price] | None = None,
    top: float = 48.0,
    levers: dict[str, LeverState] | None = None,
    memory: Memory | None = None,
    **kw: object,
) -> Situation:
    resolver = Resolver(intents, LEVELS, Calendar("UTC"), {ROOM: CS})
    force = resolver.in_force(NOW)
    return Situation(
        now=NOW,
        zone=UTC,
        force=force,
        ahead={},
        deadlines=resolver.deadlines(NOW, NOW + timedelta(hours=36)),
        caps=CAPS,
        prices=prices() if price is None else price,
        rooms=[RoomReading(ROOM, CS, temp, age_s, 3600.0)],
        tanks={TANK: Tank(TANK, top, 45.0, 50.0)},
        levers=levers
        if levers is not None
        else {
            OFFSET: LeverState(OFFSET, "setting", -10, 10, 1, current=0, baseline=0),
            BLOCK: LeverState(BLOCK, "hold"),
            BOOST: LeverState(BOOST, "trigger"),
        },
        memory=memory or Memory(),
        **kw,  # type: ignore[arg-type]
    )


def offset(decisions: list[Decision]) -> Decision:
    return next(d for d in decisions if d.lever == OFFSET)


def value(decision: Decision) -> float:
    found = decision.params["value"]
    assert isinstance(found, int | float)
    return float(found)


# --- prices -------------------------------------------------------------------------------


def test_the_price_signal() -> None:
    assert rules.signal(prices(cheap_now=True), NOW) == 1.0
    assert rules.signal(prices(cheap_now=False), NOW) == -1.0
    assert rules.signal([], NOW) is None
    flat = [replace(p, value=1.0) for p in prices()]
    assert rules.signal(flat, NOW) == 0.0


def test_a_shift_by_rank_nets_out() -> None:
    series = prices()
    day = [NOW + i * SLOT for i in range(96)]
    shifts = [round(2 * (rules.rank_signal(series, t, timedelta(hours=12)) or 0.0)) for t in day]
    assert max(shifts) == 2
    assert min(shifts) == -2
    assert abs(sum(shifts)) <= 12  # about as many raised as lowered


def test_the_cheapest_start_before_a_deadline() -> None:
    series = prices()
    deadline = NOW.replace(hour=23)
    start = rules.cheapest_start(series, NOW, deadline, 3600)
    assert start == NOW.replace(hour=13)


# --- heating ------------------------------------------------------------------------------


def test_a_cold_room_gets_more_heat_first() -> None:
    d = offset(rules.plan(situation([band()], temp=19.5)))
    assert value(d) > 0
    assert d.rank == 2  # comfort's low edge
    assert "below its 20.5 °C" in d.reason


def test_cheap_hours_aim_high_in_the_band_and_expensive_ones_low() -> None:
    wide = band("wide")
    cheap = offset(rules.plan(situation([wide, stance()], temp=21.5, price=prices(cheap_now=True))))
    expensive = offset(
        rules.plan(situation([wide, stance()], temp=21.5, price=prices(cheap_now=False)))
    )
    assert value(cheap) > value(expensive)
    assert cheap.rank == expensive.rank == RANK_SLIDER
    assert "cheap hours" in cheap.reason
    assert "expensive hours" in expensive.reason


def test_without_a_cost_stance_price_moves_nothing() -> None:
    cheap = offset(rules.plan(situation([band()], price=prices(cheap_now=True))))
    expensive = offset(rules.plan(situation([band()], price=prices(cheap_now=False))))
    assert cheap.params == expensive.params


def test_an_old_reading_counts_less() -> None:
    fresh = offset(rules.plan(situation([band()], temp=19.5, age_s=60)))
    old = offset(rules.plan(situation([band()], temp=19.5, age_s=2700)))
    assert 0 < value(old) < value(fresh)


def test_no_fresh_room_puts_the_offset_back() -> None:
    changed = LeverState(OFFSET, "setting", -10, 10, 1, current=3, baseline=0)
    found = rules.plan(situation([band()], temp=None, levers={OFFSET: changed}))
    assert [(d.lever, d.op) for d in found] == [(OFFSET, "restore")]
    assert rules.plan(situation([band()], temp=None)) == []  # as found already


def test_quiet_hours_raise_nothing_unless_a_room_is_cold() -> None:
    quiet = kinds.quiet_hours([((), None, None)], principal="user:a", created=BEFORE)
    warm = offset(
        rules.plan(situation([band("wide"), stance(), quiet], temp=21.5, price=prices(True)))
    )
    assert value(warm) <= 0
    assert "quiet hours" in warm.reason
    cold = offset(rules.plan(situation([band(), quiet], temp=19.0)))
    assert value(cold) > 0


def test_the_heating_stop_leaves_the_offset_to_the_pump() -> None:
    changed = LeverState(OFFSET, "setting", -10, 10, 1, current=2, baseline=0)
    found = rules.plan(situation([band()], temp=19.0, levers={OFFSET: changed}, heating=False))
    assert [(d.lever, d.op) for d in found] == [(OFFSET, "restore")]


def test_without_a_room_sensor_price_shifts_within_the_bound() -> None:
    shift = kinds.no_sensor_band(CS, steps=2, principal="user:a", created=BEFORE)
    no_rooms = replace(CAPS, rooms={})

    def at(cheap: bool) -> float:
        sit = situation([shift, stance()], price=prices(cheap_now=cheap))
        found = rules.plan(replace(sit, rooms=[], caps=no_rooms))
        return value(offset(found))

    assert at(True) == 2
    assert at(False) == -2


def test_a_slow_system_without_a_room_sensor_heats_ahead_of_the_weather() -> None:
    shift = kinds.no_sensor_band(CS, steps=2, principal="user:a", created=BEFORE)
    no_rooms = replace(CAPS, rooms={})

    def at(intents: list[Intent], lead: int, cheap: bool = True) -> Decision:
        sit = situation(intents, price=prices(cheap_now=cheap))
        return offset(
            rules.plan(replace(sit, rooms=[], caps=no_rooms, lead={CS: (lead, -8.0, 9.0)}))
        )

    alone = at([shift], 2)
    assert value(alone) == 2
    assert "-8 °C expected in 9 hours" in alone.reason
    assert value(at([shift, stance()], 2)) == 2  # within the household's bound
    assert value(at([shift, stance()], 2, cheap=False)) == 0
    without = rules.plan(replace(situation([shift]), rooms=[], caps=no_rooms))
    assert not [d for d in without if d.lever == OFFSET and d.op == "set"]


def test_the_integral_builds_while_a_room_stays_cold() -> None:
    memory = Memory(last_round=NOW - timedelta(minutes=15))
    rules.plan(situation([band()], temp=20.0, memory=memory))
    first = memory.integral[CS]
    memory.last_round = NOW - timedelta(minutes=15)
    rules.plan(situation([band()], temp=20.0, memory=memory))
    assert memory.integral[CS] > first > 0


# --- hot water ----------------------------------------------------------------------------


def test_the_floor_releases_a_hold_and_charges_below_it() -> None:
    held = {BLOCK: LeverState(BLOCK, "hold", held=True), BOOST: LeverState(BOOST, "trigger")}
    near = rules.plan(situation([floor(40.0)], top=41.5, levers=held))
    assert [(d.lever, d.op, d.rank) for d in near] == [(BLOCK, "release", 1)]
    below = rules.plan(situation([floor(40.0)], top=39.0, levers=held))
    assert [(d.op, d.rank) for d in below] == [("release", 1), ("fire", 1)]
    charging = rules.plan(situation([floor(40.0)], top=39.0, levers=held, demand="dhw"))
    assert [d.op for d in charging] == ["release"]


def test_a_deadline_is_charged_for_in_its_cheapest_window() -> None:
    by = kinds.hot_water_by(TANK, [(50.0, (), time(23, 0))], **who())  # type: ignore[arg-type]
    series = prices()
    start = rules.cheapest_start(series, NOW, NOW.replace(hour=23), rules.CHARGE_S)
    assert start == NOW.replace(hour=13)
    early = rules.plan(situation([by], top=45.0, price=series))
    assert all(d.op != "fire" for d in early)
    then = rules.plan(replace(situation([by], top=45.0, price=series), now=start))
    fired = [d for d in then if d.op == "fire"]
    assert len(fired) == 1
    assert "hot water 50 °C by 23:00: the cheapest time before it" in fired[0].reason


def test_the_grid_companys_limit_holds_a_charge_back() -> None:
    by = kinds.hot_water_by(TANK, [(50.0, (), time(23, 0))], **who())  # type: ignore[arg-type]
    series = prices()
    start = rules.cheapest_start(series, NOW, NOW.replace(hour=23), rules.CHARGE_S)
    assert start is not None
    sit = replace(situation([by], top=45.0, price=series), now=start, house_kw=4.0)
    assert any(d.op == "fire" for d in rules.plan(sit))
    held = replace(sit, grid_limit=GridLimit(5.0, "A grid company: the subscribed power"))
    assert all(d.op != "fire" for d in rules.plan(held))  # 4 kW and a charge's 2 is over 5


def test_a_learned_draw_replaces_the_guess_under_a_limit() -> None:
    by = kinds.hot_water_by(TANK, [(50.0, (), time(23, 0))], **who())  # type: ignore[arg-type]
    series = prices()
    start = rules.cheapest_start(series, NOW, NOW.replace(hour=23), rules.CHARGE_S)
    assert start is not None
    sit = replace(situation([by], top=45.0, price=series), now=start, house_kw=4.0)
    held = replace(sit, grid_limit=GridLimit(5.0, "A grid company: the subscribed power"))
    assert all(d.op != "fire" for d in rules.plan(held))  # 4 kW and the guess's 2 is over 5
    small = replace(held, charge_kw=0.8)
    assert any(d.op == "fire" for d in rules.plan(small))  # 4.8 kW isn't


def test_a_late_deadline_charges_at_once() -> None:
    by = kinds.hot_water_by(TANK, [(50.0, (), time(12, 45))], **who())  # type: ignore[arg-type]
    found = rules.plan(situation([by], top=44.0))
    assert [d.op for d in found if d.lever == BOOST] == ["fire"]
    assert "starting now to make it" in next(d.reason for d in found if d.lever == BOOST)


def test_no_charge_where_the_tank_will_make_it() -> None:
    by = kinds.hot_water_by(TANK, [(45.0, (), time(13, 0))], **who())  # type: ignore[arg-type]
    assert all(d.op != "fire" for d in rules.plan(situation([by], top=52.0)))


def test_the_periodic_increase_is_told_by_its_own_stop_at_the_charge_sensor() -> None:
    # As on a pump whose mode changes in the day: after a charge in a warmer mode, economy's
    # stop of 45 °C and the top at 48.5 °C. That is no periodic increase.
    economy = Tank(TANK, 48.5, 41.0, 45.0, charge=44.0, periodic_stop=55.0)
    assert not rules.periodic_seen(economy)
    assert not rules.periodic_seen(replace(economy, charge=51.0))  # a luxury charge's end
    assert rules.periodic_seen(replace(economy, top=59.5, charge=54.5))
    # Without its own stop, or with one an ordinary charge reaches, it isn't told at all.
    assert not rules.periodic_seen(replace(economy, charge=56.0, periodic_stop=None))
    assert not rules.periodic_seen(replace(economy, stop=54.0, charge=56.0))


def test_the_periodic_increase_makes_a_charge_needless() -> None:
    by = kinds.hot_water_by(TANK, [(50.0, (), time(12, 45))], **who())  # type: ignore[arg-type]
    memory = Memory(
        periodic={
            TANK: NOW - timedelta(days=7) + timedelta(minutes=30),
            f"{TANK}#before": NOW - timedelta(days=14) + timedelta(minutes=30),
        }
    )
    assert all(d.op != "fire" for d in rules.plan(situation([by], top=44.0, memory=memory)))


def test_expensive_hours_hold_the_charges_off_while_the_tank_is_well_above_its_floor() -> None:
    expensive = prices(cheap_now=False)
    held = rules.plan(situation([floor(40.0), stance()], top=48.0, price=expensive))
    assert [(d.lever, d.op) for d in held] == [(BLOCK, "engage")]
    low = rules.plan(situation([floor(40.0), stance()], top=42.5, price=expensive))
    assert all(d.lever != BLOCK for d in low)
    no_stance = rules.plan(situation([floor(40.0)], top=48.0, price=expensive))
    assert all(d.lever != BLOCK for d in no_stance)


def test_a_hold_is_released_once_prices_ease() -> None:
    held = {BLOCK: LeverState(BLOCK, "hold", held=True), BOOST: LeverState(BOOST, "trigger")}
    memory = Memory(blocked={TANK: True})
    found = rules.plan(
        situation([floor(40.0), stance()], levers=held, memory=memory, price=prices(True))
    )
    assert [(d.lever, d.op) for d in found] == [(BLOCK, "release")]


def test_a_block_held_the_longest_is_let_go_until_the_pump_has_charged() -> None:
    expensive = prices(cheap_now=False)
    intents = [floor(40.0), stance()]
    held = {BLOCK: LeverState(BLOCK, "hold", held=True), BOOST: LeverState(BOOST, "trigger")}
    spent = Memory(blocked={TANK: True}, spent={TANK: NOW})
    found = rules.plan(situation(intents, levers=held, memory=spent, price=expensive))
    assert [(d.lever, d.op, d.reason) for d in found] == [
        (BLOCK, "release", "held long enough: the pump may charge again")
    ]
    free = {BLOCK: LeverState(BLOCK, "hold"), BOOST: LeverState(BOOST, "trigger")}
    again = rules.plan(
        situation(intents, levers=free, memory=Memory(spent={TANK: NOW}), price=expensive)
    )
    assert all(d.lever != BLOCK for d in again)


def test_boost_now() -> None:
    found = rules.plan(situation([], boost_scopes=frozenset({TANK})))
    assert [(d.lever, d.op) for d in found] == [(BOOST, "fire")]


# --- what holds everything back -----------------------------------------------------------


def test_hands_off_asks_for_nothing() -> None:
    hands_off = kinds.hands_off(NOW + timedelta(hours=2), principal="user:a", created=NOW)
    assert rules.plan(situation([band(), floor(40.0), hands_off], temp=18.0, top=30.0)) == []


def test_a_spent_budget_leaves_only_the_top_ranks() -> None:
    expensive = prices(cheap_now=False)
    spent = rules.plan(situation([band(), floor(40.0), stance()], price=expensive, budget=(50, 50)))
    assert spent == []
    cold = rules.plan(situation([band(), floor(40.0)], temp=19.0, budget=(50, 50)))
    assert [d.lever for d in cold] == [OFFSET]


def test_the_addition_by_policy() -> None:
    stop = "pump:hp1/addition/stop_temp"
    power = "pump:hp1/addition/max_power"
    levers = {
        stop: LeverState(stop, "setting", -25, 40, 1, current=5, baseline=5),
        power: LeverState(power, "setting", 0, 45, 0.5, current=6, baseline=6),
    }
    expensive = prices(cheap_now=False)
    not_expensive = kinds.addition_policy("not_when_expensive", **who())  # type: ignore[arg-type]
    found = rules.plan(situation([not_expensive], levers=levers, price=expensive))
    # Kept off at -15 °C, not the lever's -25: left there, it still helps in real cold.
    assert [(d.lever, d.params) for d in found] == [(stop, {"value": -15})]
    cold = rules.plan(situation([not_expensive, band()], temp=19.0, levers=levers, price=expensive))
    assert all(d.lever != stop for d in cold)
    limit = kinds.addition_policy("limit", kw=3.0, **who())  # type: ignore[arg-type]
    found = rules.plan(situation([limit], levers=levers))
    assert [(d.lever, d.params) for d in found] == [(power, {"value": 3.0})]


def test_an_aired_room_isnt_heated_harder() -> None:
    sit = situation([band()], temp=19.0)
    aired = replace(sit, rooms=[replace(sit.rooms[0], airing=True)])
    assert rules.plan(aired) == []
    assert value(offset(rules.plan(sit))) > 0


def test_a_pool_by_price() -> None:
    pool = "pump:hp1/pool1"
    levels = {**LEVELS, "warm": Level(id="warm", name="Warm", scope=pool, low=26.0, high=29.0)}
    intent = kinds.pool(pool, "warm", **who())  # type: ignore[arg-type]
    start, stop = f"{pool}/start_temp", f"{pool}/stop_temp"
    levers = {
        start: LeverState(start, "setting", 5, 80, 0.5, current=26, baseline=26),
        stop: LeverState(stop, "setting", 5, 80, 0.5, current=29, baseline=29),
    }
    resolver = Resolver([intent, stance()], levels, Calendar("UTC"))

    def at(cheap: bool) -> dict[str, object]:
        sit = situation([], levers=levers, price=prices(cheap_now=cheap))
        sit = replace(sit, force=resolver.in_force(NOW), pools={pool: 27.0})
        return {d.lever: (d.op, d.params.get("value")) for d in rules.plan(sit)}

    cheap, expensive = at(True), at(False)
    assert cheap[start] == ("set", 28.5)  # warmed toward the top
    assert expensive[start] == ("set", 26.0)  # held at the band's bottom, never switched off
