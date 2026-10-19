"""The hot-water tank learned from its two temperatures and the pump's demand: against the
simulated house's tank, whose draws and standing loss are known, and piece by piece."""

from datetime import UTC, date, datetime, timedelta
from functools import cache
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from zoneinfo import ZoneInfo

import pytest
from plant import begin, draw

from thermaestro.core import AuditLog, Values
from thermaestro.core.values import Key, Sample
from thermaestro.learn import Learned, Learnings, water
from thermaestro.learn import tank as t
from thermaestro.learn.hotwater import DEMAND, LOSS, HotWater, expected
from thermaestro.learn.inputs import Check
from thermaestro.learn.service import KEPT, _kept_proof
from thermaestro.planner.model import Tank
from thermaestro.planner.rules import TANK_LOSS_C_H, expected_top
from thermaestro.store import Database

UTC_ZONE = ZoneInfo("UTC")
DAYS = 9


@cache
def recorded(seed: int = 3, days: int = DAYS) -> tuple[Any, dict[str, list[Sample]]]:
    """The plant's tank, a reading a minute, as the pump would show it."""
    scenario = draw(seed, start=datetime(2026, 1, 12, tzinfo=UTC))
    plant = begin(scenario)
    out: dict[str, list[Sample]] = {"top": [], "low": [], "demand": []}
    for _ in range(days * 24 * 60):
        out["top"].append(Sample(plant.t, round(plant.top, 1), None, "good"))
        out["low"].append(Sample(plant.t, round(plant.bottom, 1), None, "good"))
        out["demand"].append(Sample(plant.t, None, plant.demand, "good"))
        plant.run(60)
    return scenario, out


def truth(scenario: Any, day: date) -> list[tuple[datetime, float]]:
    """The scenario's draws on a day: when, and how many liters."""
    start = datetime.combine(day, datetime.min.time(), UTC)
    return [(start + timedelta(hours=d.hour), d.liters) for d in scenario.draws]


def read(scenario: Any, series: dict[str, list[Sample]], day: date) -> t.Day:
    top, low = t.Series(series["top"]), t.Series(series["low"])
    start, end = t.bounds(day, UTC_ZONE)
    found = t.charges(series["demand"], end + 3600, top)
    hours = t.steps(top, low, found, start - 7 * 86_400, end, step=3600)
    k = t.loss_rate(hours) or 0.0
    quarters = t.steps(top, low, found, start - 7 * 86_400, end)
    return t.read_day(day, UTC_ZONE, top, low, found, k, t.threshold(quarters, k), 1.0)


def test_the_standing_loss_is_found() -> None:
    scenario, series = recorded()
    top, low = t.Series(series["top"]), t.Series(series["low"])
    end = series["top"][-1].t
    found = t.charges(series["demand"], end, top)
    k = t.loss_rate(t.steps(top, low, found, series["top"][0].t, end, step=3600))
    assert k is not None
    # The plant: 1.5 W/K over 180 l of water, about 0.0072 an hour; the room is 18 °C.
    assert k == pytest.approx(scenario.tank.loss_w_k * 3600 / (180 * 4186), rel=0.5)


def test_the_draws_are_found_where_they_were() -> None:
    scenario, series = recorded()
    found = missed = extra = 0
    for n in range(2, DAYS - 1):
        day = date(2026, 1, 12) + timedelta(days=n)
        seen = read(scenario, series, day).draws
        real = [(w, liters) for w, liters in truth(scenario, day) if liters >= 12]
        for when, _ in real:
            near = [d for d in seen if abs(d.at - when.timestamp()) <= 3600]
            found += bool(near)
            missed += not near
        extra += sum(
            1
            for d in seen
            if all(abs(d.at - w.timestamp()) > 3600 for w, _ in truth(scenario, day))
        )
    assert found >= 0.8 * (found + missed), (found, missed)
    assert extra <= DAYS, extra


def test_a_bigger_draw_counts_for_more() -> None:
    scenario, series = recorded()
    sizes: dict[float, list[float]] = {}
    for n in range(2, DAYS - 1):
        day = date(2026, 1, 12) + timedelta(days=n)
        seen = read(scenario, series, day).draws
        for when, liters in truth(scenario, day):
            near = [d.size for d in seen if abs(d.at - when.timestamp()) <= 3600]
            if near:
                sizes.setdefault(liters, []).append(max(near))
    small = [s for liters, found in sizes.items() if liters < 20 for s in found]
    large = [s for liters, found in sizes.items() if liters > 35 for s in found]
    assert small, "no small draw found"
    assert large, "no large draw found"
    assert sum(large) / len(large) > 1.5 * sum(small) / len(small)


def test_a_draw_that_starts_a_charge_counts_up_to_it() -> None:
    q = 900.0
    quarters = [(0.0, q, 50.0, 49.9), (q, 2 * q, 49.9, 47.0), (2 * q, 2 * q + 300, 47.0, 44.0)]
    found = t.draws(quarters, 0.0, 0.5)
    assert len(found) == 1
    assert found[0].size == pytest.approx(5.9, abs=0.01)
    assert found[0].end == 2 * q + 300
    after = [*quarters, (3 * q, 4 * q, 47.0, 44.0)]  # after the charge: another draw
    assert len(t.draws(after, 0.0, 0.5)) == 2


def test_the_week_learns_day_by_day() -> None:
    week = t.Demand()
    week.learn(0, [1.0] * 24)
    week.learn(0, [3.0] * 24)
    assert week.day(0)[0] == pytest.approx(2.0)  # a running mean at first
    for _ in range(20):
        week.learn(0, [0.0] * 24)
    assert week.day(0)[0] < 0.3  # then a weight of a tenth, so it forgets
    again = t.Demand.from_value(week.to_value())
    assert again.hours == pytest.approx(week.hours, abs=1e-3)
    assert again.seen == week.seen


def test_what_is_expected_follows_the_week_and_the_holidays() -> None:
    week = t.Demand()
    week.learn(6, [0.0] * 7 + [4.0] + [0.0] * 16)  # Sunday 07:00
    monday = datetime(2026, 12, 21, tzinfo=UTC)
    christmas = date(2026, 12, 25)

    def weekday(d: date) -> int:
        return 6 if d == christmas else d.weekday()

    ahead = t.expected(week, weekday, UTC_ZONE, lambda d: 1.0, monday, 5 * 24)
    assert ahead[4 * 24 + 7] == 4.0  # Friday the 25th counts as a Sunday
    assert sum(ahead) == 4.0
    doubled = t.expected(week, weekday, UTC_ZONE, lambda d: 2.0, monday, 5 * 24)
    assert sum(doubled) == 8.0


def test_the_cold_water_follows_the_seasons() -> None:
    assert water.inlet(date(2026, 2, 1), 7.0, 18.0, well=True) == 7.0
    winter = water.inlet(date(2026, 2, 15), 7.0, 18.0, well=False)
    summer = water.inlet(date(2026, 8, 15), 7.0, 18.0, well=False)
    assert winter < 8 < summer
    assert water.season(date(2026, 2, 15), 7.0, 18.0, well=False) > 1
    assert water.season(date(2026, 8, 15), 7.0, 18.0, well=False) < 1
    south = water.season(date(2026, 8, 15), 7.0, 18.0, well=False, south=True)
    assert south > 1


def test_a_proof_is_kept_through_a_quiet_week() -> None:
    proved = Check("dev", "hp1/addition/power", "usable", "Behaved as it should.", 100.0)
    quiet = Check("dev", "hp1/addition/power", "not_yet", "Not seen.", 200.0)
    kept = _kept_proof(quiet, proved)
    assert (kept.verdict, kept.why, kept.args) == ("usable", KEPT, {"seen": 100.0})
    later = _kept_proof(Check("dev", "hp1/addition/power", "not_yet", "x", 300.0), kept)
    assert later.args == {"seen": 100.0}  # still when it was last seen
    wrong = Check("dev", "hp1/addition/power", "not_usable", "Never 0.", 300.0)
    assert _kept_proof(wrong, kept).verdict == "not_usable"


def test_the_rules_use_the_tank_once_it_has_proved_itself() -> None:
    now = datetime(2026, 1, 12, 5, 30, tzinfo=UTC)
    then = now + timedelta(hours=2)
    guessed = Tank("dev:hp1/dhw", 50.0, 45.0)
    assert expected_top(guessed, now, then) == 50.0 - 2 * TANK_LOSS_C_H
    draws = (0.0, 6.0, 0.0)  # 05:00, 06:00, 07:00
    learned = Tank("dev:hp1/dhw", 50.0, 45.0, loss_per_h=0.01, draws=draws)
    top = expected_top(learned, now, then)
    assert top == pytest.approx(50.0 - 0.01 * 30 * 2 - 6.0)


async def test_the_tank_is_learned_day_by_day_and_kept(tmp_path: Path) -> None:
    _, series = recorded()
    node = SimpleNamespace
    described = node(
        nodes=[node(path="hp1", kind="unit"), node(path="hp1/dhw", kind="dhw_tank")],
        points=[],
    )
    host = cast(Any, node(instances={"dev": node(described=described)}))
    keys = {
        "top": Key("dev", "hp1/dhw/temp.top"),
        "low": Key("dev", "hp1/dhw/temp.charge"),
        "demand": Key("dev", "hp1/demand"),
    }
    async with await Database.open(tmp_path / "t.db") as db:
        await db.run(
            lambda tr: tr.executemany(
                "INSERT INTO history (instance, point, t, value, text, quality)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                [
                    (k.instance, k.point, s.t, s.value, s.text, s.quality)
                    for name, k in keys.items()
                    for s in series[name]
                ],
            )
        )
        learnings = Learnings(db, AuditLog(tmp_path / "audit"))
        await learnings.load()
        usable = {"yes": True}
        hot = HotWater(db, Values(db), host, learnings, lambda key: usable["yes"])
        end = series["top"][-1].t
        await hot.run(end)
        week = learnings.get(DEMAND, "dev:hp1/dhw")
        assert week is not None
        assert week.n == DAYS - 1  # every whole day but today, which isn't over
        loss = learnings.get(LOSS, "dev:hp1/dhw")
        assert loss is not None
        assert isinstance(loss.value, float)
        days = await hot.events("dev:hp1/dhw", "day", 0.0)
        assert len(days) == DAYS - 1
        assert sum(b["draws"] for _, b in days) >= 2 * (DAYS - 1)
        await hot.run(end)  # nothing new to read
        assert len(await hot.events("dev:hp1/dhw", "day", 0.0)) == DAYS - 1
        # Not used until proved: the loss alone, or a week not yet judged, isn't enough.
        calendar = SimpleNamespace(weekday=lambda d: d.weekday(), zone=UTC_ZONE)
        start = datetime.fromtimestamp(end, UTC)
        assert expected(learnings, "dev:hp1/dhw", cast(Any, calendar), lambda d: 1.0, start) is None
        proved = week.model_copy(update={"state": "proved"})
        await learnings.put(proved)
        await learnings.pin(loss.key, 0.007, "user:admin")
        found = expected(learnings, "dev:hp1/dhw", cast(Any, calendar), lambda d: 1.0, start)
        assert found is not None
        assert found[0] == 0.007
        assert len(found[1]) == 48


async def test_a_tank_without_usable_inputs_says_why(tmp_path: Path) -> None:
    node = SimpleNamespace
    described = node(
        nodes=[node(path="hp1", kind="unit"), node(path="hp1/dhw", kind="dhw_tank")],
        points=[],
    )
    host = cast(Any, node(instances={"dev": node(described=described)}))
    async with await Database.open(tmp_path / "t.db") as db:
        learnings = Learnings(db, AuditLog(tmp_path / "audit"))
        await learnings.load()
        hot = HotWater(
            db, Values(db), host, learnings, lambda key: key.point != "hp1/dhw/temp.charge"
        )
        await hot.run(2_000_000_000.0)
        found = learnings.get(DEMAND, "dev:hp1/dhw")
        assert found is not None
        assert found.why == "Not learned: %(what)s isn't usable on this pump yet."
        assert found.args == {"what": "the tank's charge sensor"}
        assert found.used is None


def test_a_learned_value_carries_its_reasons_values() -> None:
    x = Learned(name="tank.demand", scope="s", kind="model", why="%(n)s days", args={"n": 3})
    assert x.args == {"n": 3}


async def test_a_day_not_seen_is_not_learned_as_one_without_use(tmp_path: Path) -> None:
    """Thermaestro down for a day: that day is marked read, not learned as no hot water."""
    _, series = recorded()
    node = SimpleNamespace
    described = node(
        nodes=[node(path="hp1", kind="unit"), node(path="hp1/dhw", kind="dhw_tank")],
        points=[],
    )
    host = cast(Any, node(instances={"dev": node(described=described)}))
    keys = {
        "top": Key("dev", "hp1/dhw/temp.top"),
        "low": Key("dev", "hp1/dhw/temp.charge"),
        "demand": Key("dev", "hp1/demand"),
    }
    gone = series["top"][0].t + 5 * 86_400  # the sixth day, give or take its edges
    async with await Database.open(tmp_path / "t.db") as db:
        await db.run(
            lambda tr: tr.executemany(
                "INSERT INTO history (instance, point, t, value, text, quality)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                [
                    (k.instance, k.point, s.t, s.value, s.text, s.quality)
                    for name, k in keys.items()
                    for s in series[name]
                    if name == "demand" or not gone <= s.t < gone + 86_400
                ],
            )
        )
        learnings = Learnings(db, AuditLog(tmp_path / "audit"))
        await learnings.load()
        hot = HotWater(db, Values(db), host, learnings, lambda key: True)
        await hot.run(series["top"][-1].t)
        days = await hot.events("dev:hp1/dhw", "day", 0.0)
        skipped = [b for _, b in days if b.get("skipped")]
        assert 1 <= len(skipped) <= 2  # the day the readings stopped in, and any after
        week = learnings.get(DEMAND, "dev:hp1/dhw")
        assert week is not None
        assert week.n == len(days) - len(skipped)


async def test_the_ceiling_leaves_out_the_periodic_increase_after_a_restart(
    tmp_path: Path,
) -> None:
    """Just restarted, the tank's stop temperature isn't read again yet: the last one kept
    tells the periodic increase from a charge."""
    import json as json_
    from types import SimpleNamespace
    from typing import Any, cast

    from thermaestro.core import AuditLog
    from thermaestro.core.values import Key, Values
    from thermaestro.learn import Learnings
    from thermaestro.learn.hotwater import CEILING, HotWater, TankKeys
    from thermaestro.store import Database

    now = 2_000_000_000.0
    keys = TankKeys(
        "dev:hp1/dhw",
        Key("dev", "hp1/dhw/temp.top"),
        Key("dev", "hp1/dhw/temp.charge"),
        Key("dev", "hp1/demand"),
        Key("dev", "hp1/dhw/temp.stop"),
    )
    async with await Database.open(tmp_path / "t.db") as db:
        learnings = Learnings(db, AuditLog(tmp_path / "audit"))
        await learnings.load()

        def stock(t: Any) -> None:
            t.execute(
                "INSERT INTO history (instance, point, t, value, text, quality)"
                " VALUES ('dev', 'hp1/dhw/temp.stop', ?, 50.0, NULL, 'good')",
                (now - 7200,),
            )
            t.executemany(
                "INSERT INTO learn_events (scope, kind, t, body) VALUES (?, 'charge', ?, ?)",
                [
                    (keys.scope, now - 86_400, json_.dumps({"top1": 51.0})),
                    (keys.scope, now - 3600, json_.dumps({"top1": 65.0})),  # periodic
                ],
            )

        await db.run(stock)
        host = cast(Any, SimpleNamespace(instances={}))
        hot = HotWater(db, Values(db), host, learnings, lambda key: True)
        await hot._ceiling(keys, now)
        found = learnings.get(CEILING, keys.scope)
        assert found is not None
        assert found.value == 51.0
