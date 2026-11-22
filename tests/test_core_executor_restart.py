"""The executor after a restart, and putting back when it doesn't go as it should: what a
read after the restart says, a change in flight on a lever that can't be read, a hold
whose move fails, a baseline the dead-man can't be told, the watchdog's event."""

import asyncio
import contextlib
from collections.abc import Collection
from datetime import UTC, datetime
from pathlib import Path

import pytest
from leverfake import LeverDevice
from test_core_executor import BLOCK, OFFSET, Rig, covered, rig, until

from thermaestro.cap.messages import Op, Values
from thermaestro.core.events import Events


def fresh_reads(r: Rig) -> None:
    """A reading asked for is taken now, whatever the device pushes: with its pushes an
    hour old, what a restart finds is what a read says."""
    link = r.host.instances["dev"].link
    assert link is not None
    read = link.read

    async def fresh(
        points: Collection[str], *, after: datetime | None = None, timeout: float = 5.0
    ) -> Values:
        answer = await read(points, after=after, timeout=timeout)
        now = datetime.now(UTC)
        values = tuple(e.model_copy(update={"t_observed": now}) for e in answer.values)
        return answer.model_copy(update={"values": values})

    link.read = fresh  # type: ignore[method-assign]


async def left_set_to_2(tmp_path: Path, device: LeverDevice) -> None:
    """The offset set to 2, and the process gone without putting it back."""
    async with rig(tmp_path, {OFFSET: "control"}, device) as r:
        assert (await r.executor.act(OFFSET, "set", {"value": 2}, who="planner")).outcome == (
            "verified"
        )
        await r.executor.stop()


async def test_a_setting_put_back_by_hand_while_down_is_found_as_found(tmp_path: Path) -> None:
    """Someone set it back to what it was found at while Thermaestro was down: a read after
    the restart says so, and nothing is written, nor let go."""
    device = LeverDevice()
    await left_set_to_2(tmp_path, device)
    device.registers["x.fake.offset"] = -4
    device.stale["x.fake.offset"] = -4
    async with rig(tmp_path, {OFFSET: "control"}, device) as r:
        fresh_reads(r)
        await until(lambda: not r.executor.claims[OFFSET].changed)
        assert r.executor.claims[OFFSET].drift is None
    assert [a.params for a in device.acts] == [{"value": 2}]


async def test_a_setting_changed_while_down_is_let_go_not_written_over(tmp_path: Path) -> None:
    device = LeverDevice()
    await left_set_to_2(tmp_path, device)
    device.registers["x.fake.offset"] = 0
    device.stale["x.fake.offset"] = 0
    async with rig(tmp_path, {OFFSET: "control"}, device) as r:
        fresh_reads(r)
        await until(lambda: r.executor.claims[OFFSET].drift is not None)
        assert r.executor.claims[OFFSET].drift == "after a restart it reads 0, not 2"
        await asyncio.sleep(0.3)
    assert device.registers["x.fake.offset"] == 0
    assert [a.params for a in device.acts] == [{"value": 2}]


async def test_a_device_replaced_while_down_isnt_written_after_a_restart(tmp_path: Path) -> None:
    device = LeverDevice()
    await left_set_to_2(tmp_path, device)
    device.identity = device.identity.model_copy(update={"serial": "B2"})
    async with rig(tmp_path, {OFFSET: "control"}, device) as r:
        await until(lambda: r.executor.claims[OFFSET].drift is not None)
        assert r.executor.claims[OFFSET].drift == (
            "the device isn't the one it was taken over on: serial A1 is now B2"
        )
        await asyncio.sleep(0.3)
    assert device.registers["x.fake.offset"] == 2
    assert [a.op for a in device.acts] == ["set"]


@pytest.mark.parametrize(("op", "how"), [("engage", "unanswered"), ("release", "stalled")])
async def test_a_holds_change_in_flight_at_a_crash_is_released_after_a_restart(
    tmp_path: Path, op: Op, how: str
) -> None:
    """A hold can't be read back: a change of it in flight is taken as made, an engage as
    engaged and a release as not yet done, so the restart releases it either way."""
    device = LeverDevice()
    async with rig(tmp_path, {BLOCK: "control"}, device) as r:
        if op == "release":
            await r.executor.act(BLOCK, "engage", who="planner")
        getattr(device, how).add("hp1/block")
        acting = asyncio.create_task(r.executor.act(BLOCK, op, who="planner"))
        await until(lambda: bool(device.acts) and device.acts[-1].op == op)
        acting.cancel()  # the process stops here
        with contextlib.suppress(asyncio.CancelledError):
            await acting
        await r.executor.stop()
    getattr(device, how).clear()
    assert device.held == {"hp1/block"}
    async with rig(tmp_path, {BLOCK: "control"}, device) as r:
        await until(lambda: not r.executor.claims[BLOCK].changed)
        assert device.held == set()
    assert device.acts[-1].op == "release"


async def test_a_hold_that_cant_be_released_to_move_stays_where_it_is(tmp_path: Path) -> None:
    """What the hold acts on moved, and its release is refused: it isn't engaged again,
    so nothing is held twice, and it stays held, to be put back."""
    async with rig(tmp_path, {BLOCK: "control"}) as r:
        await r.executor.act(BLOCK, "engage", who="planner")
        r.device.refused.add("hp1/block")
        await r.device.redescribe("hp1/block")
        await until(lambda: len(r.device.acts) == 2)
        await asyncio.sleep(0.2)
        assert [a.op for a in r.device.acts] == ["engage", "release"]
        assert r.executor.claims[BLOCK].held


async def test_a_baseline_the_dead_man_cant_be_told_of_isnt_taken(tmp_path: Path) -> None:
    """No answer from its plugin in time, or its plugin down: the baseline stays as it was,
    so the dead-man and the executor put back the same."""
    async with rig(tmp_path, {OFFSET: "control"}, covered(), act_timeout_s=0.3) as r:
        await r.executor.act(OFFSET, "set", {"value": 2}, who="planner")
        r.device.unanswered.add("hp1/offset")
        untold = "the dead-man in its route couldn't be told"
        assert await r.executor.set_baseline(OFFSET, 1, who="anna") == (
            f"{untold}: the plugin didn't say what became of it"
        )
        r.device.unanswered.clear()
        await r.host.stop()
        assert await r.executor.set_baseline(OFFSET, 1, who="anna") == (
            f"{untold}: its plugin isn't running"
        )
        assert r.executor.claims[OFFSET].baseline == -4


async def test_a_watchdog_put_back_is_told_as_an_event(tmp_path: Path) -> None:
    async with rig(tmp_path, {OFFSET: "control"}) as r:
        events = r.executor.events = Events(r.db)
        await r.executor.act(OFFSET, "set", {"value": 2}, who="planner")
        r.executor.heartbeat()
        async with asyncio.timeout(5):
            while not await events.since(0, "restore.watchdog"):
                await asyncio.sleep(0.02)
        [told] = await events.since(0, "restore.watchdog")
        assert told.cleared is not None  # over as it happens
        assert r.device.registers["x.fake.offset"] == -4
