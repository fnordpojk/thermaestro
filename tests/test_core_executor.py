import asyncio
import json
import sqlite3
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager, closing
from dataclasses import dataclass
from pathlib import Path

from leverfake import LeverDevice

from thermaestro.cap.messages import Op
from thermaestro.cap.model import Value
from thermaestro.core import AuditLog, Executor, Key, PluginHost, State, Values
from thermaestro.core.executor import left_changed
from thermaestro.store import Control, Database, LeverMode, Plugin, SecretStore

OFFSET = "dev:hp1/offset"
BLOCK = "dev:hp1/block"


@dataclass
class Rig:
    db: Database
    host: PluginHost
    executor: Executor
    device: LeverDevice
    clock: list[float]
    """Seconds added to the real time: moves the executor's day and holds on."""
    audit: Path


async def until(condition: Callable[[], bool], timeout: float = 5.0) -> None:
    async with asyncio.timeout(timeout):
        while not condition():
            await asyncio.sleep(0.02)


@asynccontextmanager
async def rig(
    tmp_path: Path,
    modes: dict[str, LeverMode] | None = None,
    device: LeverDevice | None = None,
    **control: object,
) -> AsyncIterator[Rig]:
    device = device or LeverDevice()
    async with await Database.open(tmp_path / "t.db") as db:
        await db.put(Plugin(plugin="leverfake"), "dev")
        await db.put(Control.model_validate({"levers": modes or {}, **control}))
        audit = AuditLog(tmp_path / "audit")
        values = Values(db)
        host = PluginHost(
            db=db,
            secrets=SecretStore(tmp_path / "secrets.json"),
            values=values,
            audit=audit,
            factories={"leverfake": lambda c: device},
            backoff_s=(0.01, 0.05),
            timeout_s=2.0,
        )
        offset = [0.0]
        executor = Executor(
            db,
            host,
            values,
            audit,
            clock=lambda: time.time() + offset[0],
            verify_s=0.5,
            watchdog_s=0.3,
            poll_s=0.05,
        )
        await host.start()
        await until(lambda: host.instances["dev"].state is State.UP)
        await until(lambda: Key("dev", "hp1/x.fake.offset") in values.latest)
        await executor.start()
        try:
            yield Rig(db, host, executor, device, offset, tmp_path / "audit" / "audit.jsonl")
        finally:
            await executor.stop()
            await host.stop()


def acts(db_path: Path) -> list[tuple[str, str, str, str]]:
    with closing(sqlite3.connect(db_path)) as raw:
        rows = raw.execute("SELECT lever, op, params, outcome FROM acts ORDER BY id")
        return list(rows)


def audited(r: Rig) -> list[dict[str, object]]:
    return [json.loads(line) for line in r.audit.read_text().splitlines()]


async def test_a_lever_is_off_until_the_household_turns_it_on(tmp_path: Path) -> None:
    async with rig(tmp_path) as r:
        result = await r.executor.act(OFFSET, "set", {"value": 2}, who="planner")
        assert (result.outcome, result.detail) == ("refused", "this lever is off")
        assert r.device.acts == []
        assert r.executor.claims == {}


async def test_what_the_device_showed_is_kept_with_each_change(tmp_path: Path) -> None:
    """In shadow as in control: to compare what was asked with what the device had."""
    async with rig(tmp_path, {OFFSET: "shadow", BLOCK: "control"}) as r:
        await r.executor.act(OFFSET, "set", {"value": 2}, who="planner")
        await r.executor.act(BLOCK, "engage", who="planner")
        with closing(sqlite3.connect(tmp_path / "t.db")) as raw:
            found = {
                lever: json.loads(f)
                for lever, f in raw.execute(
                    "SELECT lever, found FROM acts JOIN act_found ON act_found.act = acts.id"
                )
            }
        assert {p: v["value"] for p, v in found[OFFSET].items()} == {"hp1/x.fake.offset": -4}
        assert set(found[BLOCK]) == {"hp1/x.fake.start"}


async def test_a_change_is_claimed_sent_and_read_back(tmp_path: Path) -> None:
    async with rig(tmp_path, {OFFSET: "control"}) as r:
        result = await r.executor.act(OFFSET, "set", {"value": 2}, who="planner", why="cheap")
        assert result.outcome == "verified"
        assert r.device.registers["x.fake.offset"] == 2
        claim = r.executor.claims[OFFSET]
        assert (claim.baseline, claim.last, claim.mode) == (-4, 2, "control")
        assert acts(tmp_path / "t.db") == [(OFFSET, "set", '{"value": 2}', "verified")]
        entries = [e for e in audited(r) if str(e["what"]).startswith("lever.")]
        assert [e["what"] for e in entries] == ["lever.claim", "lever.act"]
        assert entries[1]["who"] == "planner"
        assert entries[1]["why"] == "cheap"
        # The same again changes nothing and isn't sent.
        again = await r.executor.act(OFFSET, "set", {"value": 2}, who="planner")
        assert again.outcome == "unchanged"
        assert len(r.device.acts) == 1


async def test_shadow_decides_as_control_and_sends_nothing(tmp_path: Path) -> None:
    requests: list[tuple[str, Op, dict[str, Value]]] = [
        (OFFSET, "set", {"value": 2}),
        (OFFSET, "set", {"value": 3}),  # held: changed less than 15 min ago
        (BLOCK, "engage", {}),
        (BLOCK, "engage", {}),  # already engaged
        (OFFSET, "set", {"value": 11}),  # out of range
        (BLOCK, "release", {}),
    ]
    decided: dict[str, list[tuple[str, str, str]]] = {}
    modes: tuple[LeverMode, ...] = ("control", "shadow")
    for mode in modes:
        path = tmp_path / mode
        path.mkdir()
        async with rig(path, {OFFSET: mode, BLOCK: mode}) as r:
            for ref, op, params in requests:
                await r.executor.act(ref, op, params, who="planner")
            decided[mode] = [
                (lever, op, p)
                for lever, op, p, outcome in acts(path / "t.db")
                if outcome not in ("refused",)
            ]
            if mode == "shadow":
                assert r.device.acts == []
                assert r.device.registers["x.fake.offset"] == -4
                assert {o for *_, o in acts(path / "t.db")} == {"shadowed", "refused"}
    assert decided["control"] == decided["shadow"]
    assert len(decided["control"]) == 3


async def test_values_outside_what_the_lever_takes_are_refused(tmp_path: Path) -> None:
    async with rig(tmp_path, {OFFSET: "control", "dev:hp1/mode": "control"}) as r:
        await r.executor.confirm_off("dev:hp1/mode", ["the schedule"], who="anna")
        high = await r.executor.act(OFFSET, "set", {"value": 11}, who="planner")
        assert (high.outcome, high.detail) == ("refused", "11 is above 10")
        half = await r.executor.act(OFFSET, "set", {"value": 1.5}, who="planner")
        assert half.detail == "1.5 isn't in steps of 1"
        word = await r.executor.act("dev:hp1/mode", "set", {"value": "lux"}, who="planner")
        assert word.detail == "'lux' isn't one of eco, normal"
        named = await r.executor.act("dev:hp1/mode", "set", {"value": "eco"}, who="planner")
        assert named.outcome == "verified"  # the device's 0 reads back as eco
        assert r.device.registers["x.fake.mode"] == 0
        assert len(r.device.acts) == 1


async def test_what_isnt_known_well_enough_isnt_used(tmp_path: Path) -> None:
    modes: dict[str, LeverMode] = {"dev:hp1/untested": "control", "dev:hp1/mode": "control"}
    async with rig(tmp_path, modes) as r:
        untested = await r.executor.act("dev:hp1/untested", "set", {"value": 3}, who="planner")
        assert untested.detail == (
            "whether this lever works isn't known well enough to use it unattended"
        )
        competing = await r.executor.act("dev:hp1/mode", "set", {"value": "eco"}, who="planner")
        assert competing.detail == "the schedule may be on: confirm it's switched off"
        await r.executor.confirm_off("dev:hp1/mode", ["the schedule"], who="anna")
        confirmed = await r.executor.act("dev:hp1/mode", "set", {"value": "eco"}, who="planner")
        assert confirmed.outcome == "verified"
        assert [a.lever for a in r.device.acts] == ["hp1/mode"]


async def test_a_precondition_must_hold(tmp_path: Path) -> None:
    async with rig(tmp_path, {"dev:hp1/curve": "control"}) as r:
        await r.device.someone_writes("x.fake.room_control", 1, tell=False)
        await until(lambda: r.host.values.latest[Key("dev", "hp1/x.fake.room_control")].value == 1)
        refused = await r.executor.act("dev:hp1/curve", "set", {"value": 9}, who="planner")
        assert refused.detail == "hp1/x.fake.room_control == 0 doesn't hold"
        await r.device.someone_writes("x.fake.room_control", 0, tell=False)
        await until(lambda: r.host.values.latest[Key("dev", "hp1/x.fake.room_control")].value == 0)
        done = await r.executor.act("dev:hp1/curve", "set", {"value": 9}, who="planner")
        assert done.outcome == "verified"


async def test_two_levers_that_change_the_same_thing(tmp_path: Path) -> None:
    async with rig(tmp_path, {OFFSET: "control", "dev:hp1/twin": "control"}) as r:
        assert (await r.executor.act(OFFSET, "set", {"value": 1}, who="planner")).outcome == (
            "verified"
        )
        twin = await r.executor.act("dev:hp1/twin", "set", {"value": 3}, who="planner")
        assert twin.detail == "it changes the same as hp1/offset, which is taken over"


async def test_accepted_but_not_kept(tmp_path: Path) -> None:
    async with rig(tmp_path, {OFFSET: "control"}) as r:
        r.device.not_kept.add("hp1/offset")
        result = await r.executor.act(OFFSET, "set", {"value": 3}, who="planner")
        assert (result.outcome, result.detail) == ("not_kept", "accepted, but it reads -4")
        assert r.executor.claims[OFFSET].last is None  # still as found
        r.device.not_kept.clear()
        r.device.refused.add("hp1/offset")
        refused = await r.executor.act(OFFSET, "set", {"value": 3}, who="planner")
        assert refused.outcome == "device_refused"


async def test_a_setting_holds_for_a_slot(tmp_path: Path) -> None:
    async with rig(tmp_path, {OFFSET: "control"}) as r:
        await r.executor.act(OFFSET, "set", {"value": 1}, who="planner")
        soon = await r.executor.act(OFFSET, "set", {"value": 2}, who="planner")
        assert soon.detail == "changed less than 15 min ago"
        r.clock[0] += 900 - 30  # the next slot, a little short of it
        later = await r.executor.act(OFFSET, "set", {"value": 2}, who="planner")
        assert later.outcome == "verified"


async def test_the_runaway_guard(tmp_path: Path) -> None:
    async with rig(tmp_path, {OFFSET: "control"}, guard=3, min_hold_s=0) as r:
        for value in (1, 2, 3):
            assert (await r.executor.act(OFFSET, "set", {"value": value}, who="p")).outcome == (
                "verified"
            )
        stopped = await r.executor.act(OFFSET, "set", {"value": 4}, who="p")
        assert stopped.detail == "stopped by the runaway guard: 3 writes in a day"
        assert await r.executor.budget("dev") == (3, 50)
        assert any(e["outcome"] == "refused" for e in audited(r) if e["what"] == "lever.act")
        r.clock[0] += 86_401
        again = await r.executor.act(OFFSET, "set", {"value": 4}, who="p")
        assert again.outcome == "verified"


async def test_restore_puts_everything_back(tmp_path: Path) -> None:
    async with rig(tmp_path, {OFFSET: "control", BLOCK: "control"}) as r:
        await r.executor.act(OFFSET, "set", {"value": 2}, who="planner")
        engaged = await r.executor.act(BLOCK, "engage", who="planner")
        assert engaged.outcome == "awaiting_effect"
        assert engaged.detail is not None
        assert r.device.registers["x.fake.start"] == 25
        results = await r.executor.restore("Thermaestro is stopping")
        assert {ref: res.outcome for ref, res in results.items()} == {
            OFFSET: "verified",
            BLOCK: "awaiting_effect",
        }
        assert results[BLOCK].detail is None  # what engaging does isn't said of a release
        assert r.device.registers["x.fake.offset"] == -4
        assert r.device.held == set()
        assert not any(c.changed for c in r.executor.claims.values())
        restore = [e for e in audited(r) if e["what"] == "lever.restore"]
        assert restore[-1]["why"] == "Thermaestro is stopping"
        assert restore[-1]["outcome"] == "ok"


async def test_leaving_control_restores_the_lever(tmp_path: Path) -> None:
    async with rig(tmp_path, {OFFSET: "control"}) as r:
        await r.executor.act(OFFSET, "set", {"value": 2}, who="planner")
        await r.executor.set_mode(OFFSET, "shadow", who="anna")
        assert r.device.registers["x.fake.offset"] == -4
        shadowed = await r.executor.act(OFFSET, "set", {"value": 5}, who="planner")
        assert shadowed.outcome == "shadowed"
        assert r.device.registers["x.fake.offset"] == -4


async def test_a_change_someone_else_made_lets_go_of_the_lever(tmp_path: Path) -> None:
    async with rig(tmp_path, {OFFSET: "control"}) as r:
        await r.executor.act(OFFSET, "set", {"value": 2}, who="planner")
        await asyncio.sleep(0.1)
        await r.device.someone_writes("x.fake.offset", 0, tell=False)  # the pump's menu
        await until(lambda: r.executor.claims[OFFSET].drift is not None)
        assert r.executor.claims[OFFSET].drift == "it reads 0, not 2 as set"
        refused = await r.executor.act(OFFSET, "set", {"value": 1}, who="planner")
        assert (
            refused.detail
            == "let go after a change Thermaestro didn't make: it reads 0, not 2 as set"
        )
        assert await r.executor.restore("Thermaestro is stopping") == {}
        assert r.device.registers["x.fake.offset"] == 0  # never written over
        assert any(e["what"] == "lever.drift" for e in audited(r))
        await r.executor.accept_drift(OFFSET, who="anna")
        r.clock[0] += 901
        taken = await r.executor.act(OFFSET, "set", {"value": 1}, who="planner")
        assert taken.outcome == "verified"
        assert r.executor.claims[OFFSET].baseline == 0  # taken over from how it is now


async def test_another_clients_write_lets_go_of_a_hold(tmp_path: Path) -> None:
    async with rig(tmp_path, {BLOCK: "control"}) as r:
        await r.executor.act(BLOCK, "engage", who="planner")
        await r.device.someone_writes("x.fake.start", 44, tell=True)
        await until(lambda: r.executor.claims[BLOCK].drift is not None)
        assert r.executor.claims[BLOCK].drift == "another client wrote x.fake.start"


async def test_a_hold_described_anew_is_moved(tmp_path: Path) -> None:
    """What the hold acts on moved: released and engaged again, in shadow as in control."""
    modes: tuple[LeverMode, ...] = ("control", "shadow")
    for mode in modes:
        path = tmp_path / mode
        path.mkdir()
        async with rig(path, {BLOCK: mode}) as r:
            await r.device.redescribe("hp1/block")  # not held: nothing to move
            await r.executor.act(BLOCK, "engage", who="planner")
            await r.device.redescribe("hp1/block")
            async with asyncio.timeout(5):
                while len(acts(path / "t.db")) < 3:
                    await asyncio.sleep(0.02)
            outcome = "awaiting_effect" if mode == "control" else "shadowed"
            assert [(op, o) for _, op, _, o in acts(path / "t.db")] == [
                ("engage", outcome),
                ("release", outcome),
                ("engage", outcome),
            ]
            assert r.executor.claims[BLOCK].held
            sent = ["engage", "release", "engage"] if mode == "control" else []
            assert [a.op for a in r.device.acts] == sent


async def test_a_lever_kept_for_people(tmp_path: Path) -> None:
    reset = "dev:hp1/alarm.reset"
    async with rig(tmp_path, {reset: "control"}) as r:
        for who in ("planner", "core", "mqtt"):
            result = await r.executor.act(reset, "fire", who=who)
            assert (result.outcome, result.detail) == (
                "refused",
                "only a person may use this lever",
            )
        assert (await r.executor.act(reset, "fire", who="anna")).outcome == "unverifiable"
        assert [a.lever for a in r.device.acts] == ["hp1/alarm.reset"]


async def test_after_a_crash_what_was_left_changed_is_put_back(tmp_path: Path) -> None:
    device = LeverDevice()
    async with rig(tmp_path, {OFFSET: "control", "dev:hp1/curve": "control"}, device) as r:
        await r.executor.act(OFFSET, "set", {"value": 2}, who="planner")
        await r.executor.act("dev:hp1/curve", "set", {"value": 9}, who="planner")
        await r.executor.stop()  # no restore: as if the process died
    # Meanwhile someone set the curve in the menu.
    device.registers["x.fake.curve"] = 12
    async with rig(tmp_path, {OFFSET: "control", "dev:hp1/curve": "control"}, device) as r:
        await until(lambda: device.registers["x.fake.offset"] == -4)
        await until(lambda: r.executor.claims["dev:hp1/curve"].drift is not None)
        assert device.registers["x.fake.curve"] == 12  # never written over
        assert "it reads 12, not 9" in str(r.executor.claims["dev:hp1/curve"].drift)
        assert not r.executor.claims[OFFSET].changed


async def test_what_was_left_changed_and_forgetting(tmp_path: Path) -> None:
    """What a start would put back is known before it; levers as they were found, or let
    go, are forgotten, so the next request takes them over afresh."""
    curve = "dev:hp1/curve"
    async with rig(tmp_path, {OFFSET: "control", BLOCK: "control", curve: "control"}) as r:
        await r.executor.act(OFFSET, "set", {"value": 2}, who="planner")
        await r.executor.act(BLOCK, "engage", who="planner")
        await r.executor.act(curve, "set", {"value": 9}, who="planner")
        await r.device.someone_writes("x.fake.curve", 12, tell=True)
        await until(lambda: r.executor.claims[curve].drift is not None)
        assert await left_changed(r.db) == [BLOCK, OFFSET]
        await r.executor.restore("done", [OFFSET])
        await r.executor.forget()
        assert set(r.executor.claims) == {BLOCK}  # still held: kept
        assert await left_changed(r.db) == [BLOCK]
        with closing(sqlite3.connect(tmp_path / "t.db")) as raw:
            assert [lever for (lever,) in raw.execute("SELECT lever FROM claims")] == [BLOCK]


async def test_a_silent_planner_has_everything_put_back(tmp_path: Path) -> None:
    async with rig(tmp_path, {OFFSET: "control"}) as r:
        await r.executor.act(OFFSET, "set", {"value": 2}, who="planner")
        r.executor.heartbeat()
        await until(lambda: any(e["what"] == "lever.restore" for e in audited(r)), timeout=5)
        restore = [e for e in audited(r) if e["what"] == "lever.restore"]
        assert restore[-1]["why"] == "the planner stopped answering"
        assert r.device.registers["x.fake.offset"] == -4


async def test_a_newer_request_replaces_one_still_waiting(tmp_path: Path) -> None:
    async with rig(tmp_path, {OFFSET: "control"}, min_hold_s=0) as r:
        r.device.delay_s = 0.3
        first = asyncio.create_task(r.executor.act(OFFSET, "set", {"value": 1}, who="p"))
        await asyncio.sleep(0.05)
        second = asyncio.create_task(r.executor.act(OFFSET, "set", {"value": 2}, who="p"))
        await asyncio.sleep(0.05)
        third = asyncio.create_task(r.executor.act(OFFSET, "set", {"value": 3}, who="p"))
        outcomes = [(await t).outcome for t in (first, second, third)]
        assert outcomes == ["verified", "replaced", "verified"]
        assert [a.params["value"] for a in r.device.acts] == [1, 3]


async def test_a_lease_is_renewed_only_while_held(tmp_path: Path) -> None:
    async with rig(tmp_path, {"dev:hp1/leased": "control"}) as r:
        await r.executor.act("dev:hp1/leased", "set", {"value": 40}, who="planner")
        await until(lambda: sum(a.op == "renew" for a in r.device.acts) >= 2)
        await r.executor.restore("done")
        renewed = sum(a.op == "renew" for a in r.device.acts)
        await asyncio.sleep(0.6)
        assert sum(a.op == "renew" for a in r.device.acts) == renewed


async def test_a_lever_whose_plugin_is_down(tmp_path: Path) -> None:
    async with rig(tmp_path, {OFFSET: "control"}) as r:
        await r.host.stop()
        result = await r.executor.act(OFFSET, "set", {"value": 2}, who="planner")
        assert result.outcome in ("dropped", "refused")
        assert r.device.registers["x.fake.offset"] == -4


# --- A person's write, and a baseline a person sets ---------------------------------------

CURVE = "dev:hp1/curve"


async def test_a_person_writes_a_setting_outside_the_levers(tmp_path: Path) -> None:
    async with rig(tmp_path) as r:
        refused = await r.executor.write("dev", "hp1/x.fake.start", 50, who="planner")
        assert (refused.outcome, refused.detail) == (
            "refused",
            "only a person may write a device's settings",
        )
        result = await r.executor.write("dev", "hp1/x.fake.start", 50, who="admin", why="as before")
        assert result.outcome == "verified"
        assert r.device.registers["x.fake.start"] == 50
        assert [w.point for w in r.device.writes] == ["hp1/x.fake.start"]
        assert acts(tmp_path / "t.db")[-1] == (
            "dev:hp1/x.fake.start",
            "write",
            '{"value": 50}',
            "verified",
        )
        written = [e for e in audited(r) if e["what"] == "device.write"]
        assert [(e["who"], e["why"], e["outcome"]) for e in written] == [
            ("admin", "as before", "verified")
        ]


async def test_a_lever_in_control_keeps_its_setting(tmp_path: Path) -> None:
    """Changing it would only be put back: its baseline is the way."""
    async with rig(tmp_path, {CURVE: "control"}) as r:
        assert (await r.executor.act(CURVE, "set", {"value": 9}, who="planner")).outcome == (
            "verified"
        )
        result = await r.executor.write("dev", "hp1/x.fake.curve", 5, who="admin")
        assert (result.outcome, result.detail) == (
            "refused",
            "hp1/curve is in control: change its baseline instead",
        )
        assert r.device.writes == []


async def test_a_write_under_a_lever_in_shadow_starts_it_afresh(tmp_path: Path) -> None:
    async with rig(tmp_path, {CURVE: "shadow"}) as r:
        await r.executor.act(CURVE, "set", {"value": 9}, who="planner")
        assert r.executor.claims[CURVE].baseline == 7
        assert (await r.executor.write("dev", "hp1/x.fake.curve", 5, who="admin")).outcome == (
            "verified"
        )
        assert CURVE not in r.executor.claims
        await until(lambda: r.host.values.latest[Key("dev", "hp1/x.fake.curve")].value == 5)
        await r.executor.act(CURVE, "set", {"value": 9}, who="planner")
        assert r.executor.claims[CURVE].baseline == 5


async def test_a_baseline_a_person_sets_is_what_is_put_back(tmp_path: Path) -> None:
    async with rig(tmp_path, {CURVE: "control"}) as r:
        # Before Thermaestro has taken it over: kept, and used when it does.
        assert await r.executor.set_baseline(CURVE, 8, who="admin") is None
        assert r.executor.claims[CURVE].baseline == 8
        assert await r.executor.set_baseline(CURVE, 20, who="admin") == "20 is above 15"
        assert await r.executor.set_baseline(BLOCK, 1, who="admin") == (
            "only a setting has a baseline"
        )
        assert (await r.executor.act(CURVE, "set", {"value": 10}, who="planner")).outcome == (
            "verified"
        )
        assert r.executor.claims[CURVE].baseline == 8
        await r.executor.restore("stopping")
        assert r.device.registers["x.fake.curve"] == 8
        changes = [e["details"] for e in audited(r) if e["what"] == "lever.baseline"]
        assert changes == [{"lever": CURVE, "from": None, "to": 8}]
