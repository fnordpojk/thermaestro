"""The write path's checks on a request, one refusal at a time; a change whose read-back never
comes; and a person's write that can't be made or checked."""

import time
from pathlib import Path

import pytest
from leverfake import WORKS, LeverDevice, number
from test_core_executor import BLOCK, OFFSET, audited, rig, until

from thermaestro.cap.model import CompetingFeature, Knowledge, Lever, Param, Value, Verify
from thermaestro.core.executor import Claim

REPORTED = {"known": "reported", "basis": "a forum post"}
DOCUMENTED = {"known": "documented", "basis": "the fake's manual"}


def offset(**changes: object) -> Lever:
    """The fake's offset lever, with these fields changed."""
    lever = Lever(
        path="hp1/offset",
        kind="setting",
        params={"value": number(-10, 10)},
        works=WORKS,
        verify=Verify(kind="readback", point="hp1/x.fake.offset"),
        touches=("x.fake.offset",),
    )
    return lever.model_copy(update=changes)


def claimed(baseline: Value | None) -> Claim:
    return Claim(OFFSET, time.time(), baseline, "control")


UNKNOWN_RANGE = {"value": Param(type="number")}
UNKNOWN_VALUES = {
    "value": Param(type="enum", enum=Knowledge[dict[str, int]](value={"eco": 0}, **REPORTED))
}


@pytest.mark.parametrize(
    ("lever", "params", "baseline", "why"),
    [
        (offset(), {}, None, "no value given"),
        (offset(), {"value": "2"}, None, "value is a number"),
        (offset(), {"value": True}, None, "value is a number"),
        (offset(), {"value": -11}, None, "-11 is below -10"),
        (offset(), {"value": 2, "speed": 1}, None, "speed isn't a parameter of this lever"),
        (
            offset(params=UNKNOWN_RANGE),
            {"value": 3},
            None,
            "3 hasn't been seen on this device, and its range isn't known",
        ),
        (offset(params=UNKNOWN_RANGE), {"value": 3}, 3, None),
        (
            offset(params=UNKNOWN_VALUES),
            {"value": "eco"},
            None,
            "'eco' hasn't been seen on this device, and its values aren't known",
        ),
        (offset(params=UNKNOWN_VALUES), {"value": "eco"}, "eco", None),
    ],
    ids=[
        "none-given",
        "text",
        "bool",
        "below",
        "unknown-param",
        "range-unknown",
        "range-unknown-seen",
        "values-unknown",
        "values-unknown-seen",
    ],
)
async def test_what_a_value_must_be_to_be_taken(
    tmp_path: Path,
    lever: Lever,
    params: dict[str, Value],
    baseline: Value | None,
    why: str | None,
) -> None:
    async with rig(tmp_path) as r:
        claim = claimed(baseline) if baseline is not None else None
        assert r.executor._fits(lever, params, claim) == why


@pytest.mark.parametrize(
    ("lever", "why"),
    [
        (offset(unavailable="not over this route"), "not over this route"),
        (
            offset(
                competing_features=(
                    CompetingFeature(
                        name="the schedule", can_disable=Knowledge[bool](value=False, **DOCUMENTED)
                    ),
                )
            ),
            "the schedule can't be switched off",
        ),
        (
            offset(preconditions=Knowledge[tuple[str, ...]](value=("hp1/x == 0",), **REPORTED)),
            "its preconditions aren't known well enough",
        ),
    ],
    ids=["unavailable", "competing-for-good", "preconditions-not-known"],
)
async def test_a_lever_that_may_not_be_used(tmp_path: Path, lever: Lever, why: str) -> None:
    async with rig(tmp_path) as r:
        control = await r.executor._control()
        refusal = r.executor._check(OFFSET, lever, "set", {"value": 1}, control, False, "planner")
        assert refusal == why


async def test_a_lever_is_asked_only_what_its_kind_takes(tmp_path: Path) -> None:
    async with rig(tmp_path, {BLOCK: "control"}) as r:
        result = await r.executor.act(BLOCK, "set", {"value": 1}, who="planner")
        assert (result.outcome, result.detail) == ("refused", "a hold lever doesn't take set")
        assert r.device.acts == []


async def test_a_levers_own_shortest_interval_counts_when_longer(tmp_path: Path) -> None:
    async with rig(tmp_path) as r:
        control = await r.executor._control()
        lever = offset(min_interval_s=Knowledge[float](value=3600.0, **DOCUMENTED))
        claim = claimed(-4)
        claim.last_t = time.time() - 1800  # past the 15 min hold, not the lever's hour
        assert r.executor._limits(lever, claim, control, 0) == "changed less than 60 min ago"


async def test_a_lever_no_plugin_describes(tmp_path: Path) -> None:
    gone = "dev:hp1/nothing"
    async with rig(tmp_path, {gone: "control"}) as r:
        result = await r.executor.act(gone, "set", {"value": 1}, who="planner")
        assert (result.outcome, result.detail) == (
            "dropped",
            "no such lever, or its plugin isn't running",
        )


async def test_a_change_whose_read_back_never_comes_fresh(tmp_path: Path) -> None:
    async with rig(tmp_path, {OFFSET: "control"}) as r:
        r.device.stale["x.fake.offset"] = 2  # read an hour ago, whatever it holds now
        result = await r.executor.act(OFFSET, "set", {"value": 2}, who="planner")
        assert (result.outcome, result.detail) == ("timeout", "no fresh value came back")


# --- a setting's baseline, a change let go ------------------------------------------------


async def test_a_baseline_that_cant_be_set(tmp_path: Path) -> None:
    async with rig(tmp_path, {OFFSET: "control"}) as r:
        missing = await r.executor.set_baseline("dev:hp1/nothing", 1, who="anna")
        assert missing == "no such lever, or its plugin isn't running"
        await r.executor.act(OFFSET, "set", {"value": 2}, who="planner")
        await r.device.someone_writes("x.fake.offset", 5, tell=True)
        await until(lambda: r.executor.claims[OFFSET].drift is not None)
        refused = await r.executor.set_baseline(OFFSET, 1, who="anna")
        assert (
            refused
            == "let go after a change Thermaestro didn't make: another client wrote x.fake.offset"
        )


async def test_a_baseline_set_to_what_thermaestro_set_makes_it_as_found(tmp_path: Path) -> None:
    async with rig(tmp_path, {OFFSET: "control"}) as r:
        await r.executor.act(OFFSET, "set", {"value": 2}, who="planner")
        assert await r.executor.set_baseline(OFFSET, 2, who="anna") is None
        claim = r.executor.claims[OFFSET]
        assert (claim.baseline, claim.last, claim.changed) == (2, None, False)


async def test_keeping_a_change_no_lever_has_is_nothing(tmp_path: Path) -> None:
    async with rig(tmp_path) as r:
        await r.executor.accept_drift(OFFSET, who="anna")
        assert not any(e["what"] == "lever.drift_accepted" for e in audited(r))


# --- a person's write ---------------------------------------------------------------------


async def test_a_write_to_a_plugin_not_running(tmp_path: Path) -> None:
    async with rig(tmp_path) as r:
        result = await r.executor.write("nodev", "hp1/x.fake.curve", 8, who="anna")
        assert (result.outcome, result.detail) == ("dropped", "its plugin isn't running")


async def test_a_write_to_a_plugin_that_takes_none(tmp_path: Path) -> None:
    device = LeverDevice()
    device.features = ("subscribe",)
    async with rig(tmp_path, device=device) as r:
        result = await r.executor.write("dev", "hp1/x.fake.curve", 8, who="anna")
        assert (result.outcome, result.detail) == ("refused", "its plugin doesn't take writes")
        assert device.writes == []


async def test_a_persons_writes_have_the_runaway_guard_too(tmp_path: Path) -> None:
    async with rig(tmp_path, guard=1) as r:
        first = await r.executor.write("dev", "hp1/x.fake.curve", 8, who="anna")
        assert first.outcome == "verified"
        second = await r.executor.write("dev", "hp1/x.fake.curve", 9, who="anna")
        assert (second.outcome, second.detail) == (
            "refused",
            "stopped by the runaway guard: 1 writes in a day",
        )
        assert r.device.registers["x.fake.curve"] == 8


async def test_a_write_the_plugin_drops(tmp_path: Path) -> None:
    async with rig(tmp_path) as r:
        result = await r.executor.write("dev", "hp1/x.fake.nothing", 1, who="anna")
        assert (result.outcome, result.detail) == ("dropped", "no such register")


async def test_a_write_whose_read_back_never_comes_fresh(tmp_path: Path) -> None:
    async with rig(tmp_path) as r:
        r.device.stale["x.fake.curve"] = 8
        result = await r.executor.write("dev", "hp1/x.fake.curve", 8, who="anna")
        assert (result.outcome, result.detail) == ("timeout", "no fresh value came back")


async def test_a_write_under_a_lever_let_go_is_the_households(tmp_path: Path) -> None:
    async with rig(tmp_path, {OFFSET: "control"}) as r:
        await r.executor.act(OFFSET, "set", {"value": 2}, who="planner")
        await r.device.someone_writes("x.fake.offset", 5, tell=True)
        await until(lambda: r.executor.claims[OFFSET].drift is not None)
        result = await r.executor.write("dev", "hp1/x.fake.offset", 3, who="anna")
        assert result.outcome == "verified"  # not "hp1/offset is in control"
        assert OFFSET in r.executor.claims  # still let go, for the household to settle
