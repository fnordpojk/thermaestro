"""What learning stands on: the data hygiene, the held-out check, what is kept, and the
checks of each input against how its quantity behaves."""

import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from thermaestro.core import AuditLog, Values
from thermaestro.core.values import Key, Sample
from thermaestro.learn import Learned, Learning, Learnings, NotLearned
from thermaestro.learn.hygiene import fingerprint, intervals, limited, without_jumps
from thermaestro.learn.inputs import (
    Context,
    Expect,
    Recorded,
    draws_with_compressor,
    follows_demand,
    judge,
    runs_and_stops,
    warmer_when_heating,
    warms_in_charges,
)
from thermaestro.learn.validate import held_out
from thermaestro.planner import Planner
from thermaestro.store import Database

H = 3600.0
DAY = 86_400.0
END = 2_000_000_000.0
START = END - 7 * DAY


def s(t: float, value: float | None = None, text: str | None = None, q: str = "good") -> Sample:
    return Sample(t, value, text, q)


def every(step: float, value: Callable[[float], float | str], since: float = START) -> list[Sample]:
    """A reading every `step` seconds over the week: a number, or a state's name."""
    out = []
    t = since
    while t < END:
        v = value(t)
        out.append(s(t, None, v) if isinstance(v, str) else s(t, v))
        t += step
    return out


# --- hygiene --------------------------------------------------------------------------------


def test_an_interval_counts_only_when_covered() -> None:
    rows = [s(0, 10.0), s(1800, 20.0), s(3600, 30.0), s(5400, None, q="unknown")]
    found = intervals(rows, 0, 3 * H, H, hold_s=H)
    assert found[0] is not None
    assert found[0].mean == pytest.approx(15.0)
    assert found[1] is None  # half of it is unknown
    assert found[2] is None  # nothing after: the gap isn't filled


def test_a_reading_holds_only_so_long() -> None:
    found = intervals([s(0, 5.0)], 0, 2 * H, H, hold_s=H)
    assert found[0] is not None
    assert found[1] is None


def test_nothing_after_the_end_changes_what_came_before() -> None:
    rows = [s(t * 600.0, float(t)) for t in range(12)]
    before = intervals(rows, 0, H, 600)
    after = intervals([*rows, s(H + 1, 99.0), s(H + 600, -5.0)], 0, H, 600)
    assert before == after


def test_a_jump_is_left_out_and_doesnt_hold() -> None:
    rows = [s(0, 20.0), s(600, 35.0), s(1200, 20.5)]
    cleaned = without_jumps(rows, most_per_h=5.0)
    assert [x.quality for x in cleaned] == ["good", "out_of_range", "good"]


def test_a_value_moves_only_so_far() -> None:
    assert limited(None, 10.0, 1.0) == 10.0
    assert limited(5.0, 10.0, 1.0) == 6.0
    assert limited(5.0, 4.5, 1.0) == 4.5


def test_a_changed_source_changes_the_fingerprint() -> None:
    first = fingerprint({"room": "room.living/temperature", "emitter": "slab"})
    assert first == fingerprint({"emitter": "slab", "room": "room.living/temperature"})
    assert first != fingerprint({"room": "room.bed/temperature", "emitter": "slab"})


# --- the held-out check ----------------------------------------------------------------------


def test_a_model_must_beat_its_baseline() -> None:
    actual = [20.0 + (i % 3) * 0.1 for i in range(30)]
    close = [a + 0.05 for a in actual]
    flat = [20.1] * 30
    assert held_out(close, flat, actual).proved
    assert not held_out(flat, close, actual).proved
    few = held_out(close[:5], flat[:5], actual[:5])
    assert not few.proved
    assert (few.why, few.args) == ("%(n)s of %(least)s comparisons so far.", {"n": 5, "least": 24})


# --- what is kept ----------------------------------------------------------------------------


def charge(value: float | None = 3000.0, **kw: Any) -> Learned:
    return Learned(name="tank.charge_s", scope="dev:hp1/dhw", kind="observed", value=value, **kw)


async def test_learned_values_are_kept_and_the_household_has_the_last_word(
    tmp_path: Path,
) -> None:
    audit = AuditLog(tmp_path / "audit")
    async with await Database.open(tmp_path / "t.db") as db:
        kept = Learnings(db, audit)
        await kept.load()
        first = await kept.put(charge(n=1, unit="s"))
        assert kept.used("tank.charge_s", "dev:hp1/dhw") == 3000.0
        await kept.pin(first.key, 2400.0, "user:admin")
        await kept.put(charge(3300.0, n=2, unit="s"))  # learning goes on beside the pin
        assert kept.used("tank.charge_s", "dev:hp1/dhw") == 2400.0
        over = await kept.start_over(first.key, "user:admin")
        assert (over.value, over.n, over.state, over.pinned) == (None, 0, "starting", 2400.0)
        await kept.pin(first.key, None, "user:admin")
        assert kept.used("tank.charge_s", "dev:hp1/dhw") is None
        periodic = await kept.put(
            Learned(name="tank.periodic", scope="dev:hp1/dhw", kind="observed", value={"last": 1.0})
        )
        with pytest.raises(ValueError, match="isn't one number"):
            await kept.pin(periodic.key, 5.0, "user:admin")
        with pytest.raises(NotLearned):
            await kept.start_over("nothing@here", "user:admin")
    async with await Database.open(tmp_path / "t.db") as db:
        again = Learnings(db, audit)
        await again.load()
        assert [x.key for x in again.all()] == [first.key, periodic.key]
    entries = [
        json.loads(line) for line in (tmp_path / "audit" / "audit.jsonl").read_text().splitlines()
    ]
    assert [e["what"] for e in entries] == [
        "learned.pin",
        "learned.start_over",
        "learned.pin",
    ]


def test_a_model_is_used_only_once_proved() -> None:
    model = Learned(name="house.ua", scope="dev:hp1/cs1", kind="model", value=0.2, starting=0.25)
    assert model.used == 0.25
    assert model.model_copy(update={"state": "proved"}).used == 0.2
    assert model.model_copy(update={"pinned": 0.3}).used == 0.3


async def test_a_charge_time_outlives_a_restart(tmp_path: Path) -> None:
    async with await Database.open(tmp_path / "t.db") as db:
        audit = AuditLog(tmp_path / "audit")
        values = Values(db)
        kept = Learnings(db, audit)
        await kept.load()
        nothing = cast(Any, None)
        planner = Planner(db, values, nothing, nothing, nothing, nothing, audit, learnings=kept)
        since = datetime.fromtimestamp(END, UTC)
        await planner._charged("dev:hp1/dhw", 3000.0, since)
        await planner._charged("dev:hp1/dhw", 2000.0, since)
        assert planner.memory.charge_s["dev:hp1/dhw"] == 2700.0
        again = Learnings(db, audit)
        await again.load()
        restarted = Planner(db, values, nothing, nothing, nothing, nothing, audit, learnings=again)
        restarted._remembered("dev:hp1/dhw")
        assert restarted.memory.charge_s["dev:hp1/dhw"] == 2700.0
        found = again.get("tank.charge_s", "dev:hp1/dhw")
        assert found is not None
        assert found.n == 2
        await again.start_over(found.key, "user:admin")
        restarted._remembered("dev:hp1/dhw")
        assert "dev:hp1/dhw" not in restarted.memory.charge_s  # back to the rules' default


# --- the inputs ------------------------------------------------------------------------------


class Kept(Recorded):
    def __init__(self, found: dict[Key, list[Sample]]) -> None:
        self.start, self.end = START, END
        self._found = found

    async def get(self, key: Key) -> list[Sample]:
        return self._found.get(key, [])


POINT = Key("dev", "hp1/x")
DEMAND = Key("dev", "hp1/demand")
COMPRESSOR = Key("dev", "hp1/compressor/state")
HOUSE = Key("site", "meter/grid.import.power")


def hourly_pattern(t: float) -> str:
    """Each hour: 20 minutes hot water, 20 heating, 20 idle."""
    minute = (t % H) / 60
    return "dhw" if minute < 20 else "heating" if minute < 40 else "idle"


def runs_with(t: float) -> str:
    return "stopped" if hourly_pattern(t) == "idle" else "running"


async def judged(
    point: list[Sample],
    expect: Expect | None = None,
    *,
    demand: Callable[[float], str] = hourly_pattern,
    compressor: Callable[[float], str] = runs_with,
    house: bool = False,
) -> tuple[str, str, dict[str, Any]]:
    recorded = Kept({POINT: point, DEMAND: every(300, demand), COMPRESSOR: every(300, compressor)})
    context = Context(DEMAND, COMPRESSOR, (POINT, 0.001) if house else None)
    found = await judge(POINT, expect or Expect(), recorded, context, END)
    return found.verdict, found.why, dict(found.args)


async def test_the_basics() -> None:
    assert (await judged([]))[0:2] == ("not_yet", "Nothing recorded in the last week.")
    short = await judged(every(300, lambda t: t % 7, since=END - 5 * H))
    assert short == ("not_yet", "Recorded for %(span)s; a day is needed.", {"span": "5 h"})
    zero = await judged(every(300, lambda t: 0.0))
    assert zero[:2] == ("not_usable", "Always 0 in the last week: an answer of 0 says nothing.")
    stuck = await judged(every(300, lambda t: 348.3))
    assert stuck == ("not_usable", "Always %(value)s in the last week.", {"value": 348.3})
    bad = await judged([s(START + i * 600, None, q="not_connected") for i in range(300)])
    assert bad[0] == "not_usable"
    assert bad[2] == {"qualities": "not_connected"}
    moving = await judged(every(300, lambda t: 20 + (t % 1000) / 1000))
    assert moving[0] == "usable"
    still = await judged(every(300, lambda t: 21.0), Expect(moves=False))
    assert still[0] == "usable"  # a setting, which isn't expected to move


async def test_the_compressor_runs_only_with_a_demand() -> None:
    expect = Expect(relation=follows_demand)
    point = every(300, runs_with)
    assert (await judged(point, expect))[0] == "usable"
    always = every(300, lambda t: "running" if t % 600 < 300 else "stopped")
    found = await judged(always, expect)
    assert found[0] == "not_usable"
    assert found[2] == {"share": 33}  # half of each idle third
    never = await judged(every(300, lambda t: "stopped" if t % 600 < 300 else "starting"), expect)
    assert never[:2] == ("not_yet", "The compressor wasn't seen running in the last week.")


async def test_the_tank_warms_in_charges() -> None:
    expect = Expect(relation=warms_in_charges)

    def warming(t: float) -> float:
        minute = (t % H) / 60
        return 45.0 + min(minute, 20) * 0.5 - max(0.0, minute - 20) * 0.25

    assert (await judged(every(60, warming), expect))[0] == "usable"
    flat = await judged(every(60, lambda t: 50.0 + (t % 120) / 1000), expect)
    assert flat[0] == "not_usable"
    assert flat[1] == "Rose in %(warmed)s of %(charges)s hot-water charges."
    none = await judged(every(60, warming), expect, demand=lambda t: "heating")
    assert none[:2] == ("not_yet", "No hot-water charge seen in the last week.")


async def test_the_supply_is_warmer_while_the_compressor_heats() -> None:
    expect = Expect(relation=warmer_when_heating)

    def supply(t: float) -> float:
        return {"dhw": 50.0, "heating": 35.0, "idle": 28.0}[hourly_pattern(t)] + (t % 60) / 100

    assert (await judged(every(60, supply), expect))[0] == "usable"
    same = await judged(every(60, lambda t: 30.0 + (t % 7) / 100), expect)
    assert same[0] == "not_usable"
    assert same[2] == {"kelvin": 0.0}


async def test_the_house_draws_more_while_the_compressor_runs() -> None:
    expect = Expect(relation=draws_with_compressor)

    def watts(t: float) -> float:
        return (2400.0 if runs_with(t) == "running" else 600.0) + t % 50

    assert (await judged(every(60, watts), expect, house=True))[0] == "usable"
    flat = await judged(every(60, lambda t: 600.0 + t % 50), expect, house=True)
    assert flat[0] == "not_usable"


async def test_the_addition_is_seen_before_it_counts() -> None:
    expect = Expect(moves=False, relation=runs_and_stops)
    off = await judged(every(300, lambda t: 0.0), expect)
    assert off[:2] == ("not_yet", "The addition wasn't seen running in the last week.")
    stuck = await judged(every(300, lambda t: 3.0), expect)
    assert stuck[:2] == ("not_usable", "Never 0 in the last week: never seen off.")
    runs = await judged(every(300, lambda t: 3.0 if t % DAY < H else 0.0), expect)
    assert runs[0] == "usable"


async def test_the_inputs_are_found_judged_and_kept(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    audit = AuditLog(tmp_path / "audit")
    node = SimpleNamespace
    described = SimpleNamespace(
        nodes=[
            node(path="hp1", kind="unit"),
            node(path="hp1/compressor", kind="compressor"),
            node(path="hp1/dhw", kind="dhw_tank"),
        ],
        points=[
            node(path=p)
            for p in ("hp1/demand", "hp1/compressor/state", "hp1/dhw/temp.top", "hp1/alarm")
        ],
    )
    host = cast(Any, SimpleNamespace(instances={"dev": SimpleNamespace(described=described)}))
    async with await Database.open(tmp_path / "t.db") as db:
        values = Values(db)
        rows = [
            (key, x)
            for key, found in (
                (DEMAND, every(300, hourly_pattern, since=END - 3 * DAY)),
                (COMPRESSOR, every(300, lambda t: 0.0, since=END - 3 * DAY)),
            )
            for x in found
        ]
        await db.run(
            lambda t: t.executemany(
                "INSERT INTO history (instance, point, t, value, text, quality)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                [(k.instance, k.point, x.t, x.value, x.text, x.quality) for k, x in rows],
            )
        )
        learning = Learning(db, values, host, audit)
        await learning.load()
        monkeypatch.setattr("thermaestro.clock.time", lambda: END)
        found = {(c.point, c.verdict) for c in await learning.check_inputs()}
        assert found == {
            ("hp1/demand", "usable"),
            ("hp1/compressor/state", "not_usable"),  # always 0
            ("hp1/dhw/temp.top", "not_yet"),  # nothing recorded
        }
        assert learning.usable(DEMAND)
        again = Learning(db, values, host, audit)
        await again.load()
        assert {(k.point, c.verdict) for k, c in again.checks.items()} == found


async def test_a_plugin_not_described_keeps_its_inputs_checks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Starting, or its device unreachable: what was found of it stands until it can be
    judged again."""
    audit = AuditLog(tmp_path / "audit")
    node = SimpleNamespace
    described = SimpleNamespace(
        nodes=[node(path="hp1", kind="unit")], points=[node(path="hp1/demand")]
    )
    instance = SimpleNamespace(described=described)
    host = cast(Any, SimpleNamespace(instances={"dev": instance}))
    async with await Database.open(tmp_path / "t.db") as db:
        values = Values(db)
        await db.run(
            lambda t: t.executemany(
                "INSERT INTO history (instance, point, t, value, text, quality)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                [
                    (DEMAND.instance, DEMAND.point, x.t, x.value, x.text, x.quality)
                    for x in every(300, hourly_pattern, since=END - 3 * DAY)
                ],
            )
        )
        learning = Learning(db, values, host, audit)
        await learning.load()
        monkeypatch.setattr("thermaestro.clock.time", lambda: END)
        await learning.check_inputs()
        assert learning.usable(DEMAND)
        instance.described = None  # the plugin restarting
        await learning.check_inputs()
        assert learning.usable(DEMAND)


async def test_a_learner_failing_stops_none_after_it(tmp_path: Path) -> None:
    host = cast(Any, SimpleNamespace(instances={}))
    async with await Database.open(tmp_path / "t.db") as db:
        learning = Learning(db, Values(db), host, AuditLog(tmp_path / "audit"))
        await learning.load()
        ran: list[str] = []

        async def broken(now: float | None = None) -> None:
            raise RuntimeError("a learner broke")

        async def heard(now: float | None = None) -> None:
            ran.append("forecasts")

        learning.hot_water.run = broken  # type: ignore[method-assign]
        learning.forecasts.run = heard  # type: ignore[method-assign]
        with pytest.raises(ExceptionGroup) as failed:
            await learning.learn()
        assert ran == ["forecasts"]
        assert [str(e) for e in failed.value.exceptions] == ["a learner broke"]


def test_a_charges_loss_is_added_as_known_when_judged() -> None:
    """The tank's standing loss during a charge is added when the COP is worked out, so a
    loss learned after the charge was read still counts."""
    from thermaestro.learn.efficiency import _unbody

    body = {"run_s": 1800, "supply_c": 50.0, "source_c": 2.0, "brine_ks": 9000.0}
    kept = _unbody(0.0, body | {"rise_c": 10.0, "loss_kh": 20.0}, 0.05)
    assert kept.rise_c == 11.0
    old = _unbody(0.0, body | {"rise_c": 10.5}, 0.05)  # kept before: its loss already in
    assert old.rise_c == 10.5
