"""The core against the plant in simulated time: the Nibe plugin over the in-process bus,
the plant's sensors, and the executor, through hours and days in seconds. The behavior
contracts that need no planner are here; a stand-in planner asks for changes, and shadow
decides as control would."""

import asyncio
import contextlib
import json
from collections.abc import Awaitable, Callable
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest
from plant import Draw, Plant, Scenario, begin, draw
from plant.harness import Frame, Sim, running
from plant.model import s16, signed
from plant.scenario import Emitter
from plant.vloop import simulate
from simpump import SimPump
from test_nibe_plugin import FAST_PLAIN, settings, until
from test_nibe_write import act, served
from thermaestro_gateway.server import Gateway

from thermaestro import clock
from thermaestro.core import Key
from thermaestro.nibe import profile
from thermaestro.nibe.maps import load
from thermaestro.nibe.plugin import NibePlugin
from thermaestro.store import LeverMode

SCHEDULE = "the pump's hot-water schedule (menu 2.3)"
HOT_WATER: dict[str, tuple[str, ...]] = {"dhw/block": (SCHEDULE,), "dhw/mode": (SCHEDULE,)}


def scenario(seed: int = 1, **changes: object) -> Scenario:
    return replace(draw(seed, emitter="radiators"), **changes)  # type: ignore[arg-type]


def run[T](s: Scenario, body: Callable[[], Awaitable[T]]) -> T:
    async def main() -> T:
        return await body()

    return simulate(main, s.start_time)


def audited(path: Path) -> list[dict[str, object]]:
    lines = (path / "audit" / "audit.jsonl").read_text().splitlines()
    return [json.loads(line) for line in lines]


def test_the_core_reads_the_plant(tmp_path: Path) -> None:
    s = scenario()

    async def body() -> None:
        async with running(Sim(s, tmp_path)) as sim:
            await sim.advance(3600)
            plant = sim.plant
            assert sim.value("dhw/temp.top") == pytest.approx(
                signed(plant.registers[40013]) / 10, abs=0.3
            )
            assert sim.value("outdoor.temp") is not None
            room = sim.values.latest[Key("sensors", "room.cs1/temperature")]
            assert room.value == pytest.approx(plant.indoor(), abs=0.2)
            assert sim.bus.reads > 50
            assert sim.bus.writes == []

    run(s, body)


async def test_the_plant_drives_the_simulated_pump_in_real_time(
    pump: SimPump, gateway: Gateway
) -> None:
    """The other speed: the plant behind SimPump and the Python gateway, in real time (the
    plant a minute ahead every tenth of a second), read and written by the plugin."""
    plant = begin(draw(3))
    pump.registers = plant.registers
    pump.registers32 = plant.registers32

    async def ahead() -> None:
        while True:
            await asyncio.sleep(0.1)
            plant.run(60)

    stepping = asyncio.create_task(ahead())
    p = NibePlugin(
        settings(gateway, model="F1245"),
        transport_settings={"plain_settings": FAST_PLAIN},
        identify_timeout_s=5,
    )
    try:
        async with served(p) as link:
            await until(lambda: p.envelope("hp1/dhw/temp.top").quality == "good")
            top = p.envelope("hp1/dhw/temp.top").value
            assert top == pytest.approx(signed(plant.registers[40013]) / 10, abs=1.0)
            fate = await act(link, "cs1/heating.offset", "set", {"value": 2})
            assert fate.stage == "device_accepted"
            assert signed(plant.registers[47011], 8) == 2  # the plant heats by it now
    finally:
        stepping.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await stepping


def test_the_block_holds_off_charges_for_hours(tmp_path: Path) -> None:
    """Engaged at midnight, the evening's draws take the tank below its start, and no
    charge comes until the block is released at 08:00; then one does."""
    s = scenario(draws=(Draw(hour=1.0, liters=60.0, minutes=8.0),)).set({47050: 0})

    async def body() -> None:
        sim = Sim(s, tmp_path, levers={"dhw/block": "control"}, confirmed_off=HOT_WATER)
        async with running(sim):
            engaged = await sim.act("dhw/block", "engage")
            assert engaged.outcome == "awaiting_effect"
            assert sim.plant.registers[47044] == s16(25.0)
            await sim.run(8 / 24)
            assert all(row["demand"] != "dhw" for row in sim.trace)
            assert min(float(row["bottom"]) for row in sim.trace) < 45
            released = await sim.act("dhw/block", "release")
            assert released.outcome == "awaiting_effect"
            assert sim.plant.registers[47044] == s16(45.0)
            await sim.run(2 / 24)
            assert any(row["demand"] == "dhw" for row in sim.trace[-8:])

    run(s, body)


def test_a_restart_mid_hold_puts_it_back(tmp_path: Path) -> None:
    """The process dies with the block engaged; an hour later it starts again, puts the
    start temperature back, and says so."""
    s = scenario()

    async def body() -> None:
        sim = Sim(s, tmp_path, levers={"dhw/block": "control"}, confirmed_off=HOT_WATER)
        async with running(sim):
            await sim.act("dhw/block", "engage")
            await sim.stop(restore=False)
            await sim.advance(3600)
            assert sim.plant.registers[47044] == s16(25.0)
            await sim.start()
            await sim.advance(300)
            assert sim.plant.registers[47044] == s16(45.0)
            assert not sim.executor.claims["pump:hp1/dhw/block"].held

    run(s, body)
    restores = [e for e in audited(tmp_path) if e["what"] == "lever.restore"]
    assert restores[0]["why"] == "Thermaestro restarted without putting this back"


def test_a_change_made_elsewhere_is_let_go(tmp_path: Path) -> None:
    """Another client sets the offset Thermaestro set: Thermaestro lets go and never
    writes over it, not even when it stops."""
    s = scenario()

    async def body() -> None:
        sim = Sim(s, tmp_path, levers={"cs1/heating.offset": "control"})
        async with running(sim):
            assert (await sim.act("cs1/heating.offset", "set", {"value": 2})).outcome == "verified"
            sim.bus.foreign_write(47011, 5)
            await sim.advance(600)
            claim = sim.executor.claims["pump:hp1/cs1/heating.offset"]
            assert claim.drift == "another client wrote x.nibe.47011"
        assert signed(sim.plant.registers[47011], 8) == 5
        assert [r for _, r, _ in sim.bus.writes].count(47011) == 2  # Thermaestro's, the other's

    run(s, body)


def test_accepted_but_not_kept(tmp_path: Path) -> None:
    s = scenario()

    async def body() -> None:
        sim = Sim(s, tmp_path, levers={"cs1/heating.offset": "control"})
        async with running(sim):
            assert isinstance(sim.plant, Plant)
            found = signed(sim.plant.registers[47011], 8)
            sim.plant.not_kept.add(47011)
            result = await sim.act("cs1/heating.offset", "set", {"value": 2})
            assert (result.outcome, result.detail) == (
                "not_kept",
                f"accepted, but it reads {found}",
            )

    run(s, body)


def test_a_silent_planner_has_everything_put_back(tmp_path: Path) -> None:
    s = scenario()

    async def body() -> None:
        sim = Sim(s, tmp_path, levers={"cs1/heating.offset": "control"})
        async with running(sim):
            found = signed(sim.plant.registers[47011], 8)
            sim.executor.heartbeat()
            await sim.act("cs1/heating.offset", "set", {"value": 3})
            await sim.advance(20 * 60)  # no check-in for 20 minutes
            assert signed(sim.plant.registers[47011], 8) == found

    run(s, body)
    restores = [e for e in audited(tmp_path) if e["what"] == "lever.restore"]
    assert restores[0]["why"] == "the planner stopped answering"


# --- shadow decides as control would -------------------------------------------------------

LEVERS = ("dhw/block", "dhw/boost_once", "cs1/heating.offset", "addition/stop_temp")


async def stand_in(sim: Sim) -> None:
    """A stand-in planner, by the clock and the tank's top: block hot water in the morning
    and evening peaks, boost at midday if the top is cool, raise the offset at night, keep
    the addition off in the evening."""
    hour = clock.now().hour
    peak = 6 <= hour < 9 or 17 <= hour < 20
    await sim.act("dhw/block", "engage" if peak else "release")
    top = sim.value("dhw/temp.top")
    if hour == 13 and top is not None and top < 49:
        await sim.act("dhw/boost_once", "fire")
    await sim.act("cs1/heating.offset", "set", {"value": 1 if hour < 5 else 0})
    await sim.act("addition/stop_temp", "set", {"value": -20.0 if 16 <= hour < 21 else 5.0})


def intended(sim: Sim) -> list[tuple[float, str, str, str]]:
    return [
        (r.slot, r.lever, r.op, json.dumps(r.params, sort_keys=True))
        for r in sim.requests
        if r.intended
    ]


def shadow_equals_control(path: Path, s: Scenario, days: float) -> None:
    """The same scenario in control, then in shadow on the control run's recorded plant:
    the same intended writes, slot for slot, and none at all from shadow."""
    controls: dict[str, LeverMode] = dict.fromkeys(LEVERS, "control")
    shadows: dict[str, LeverMode] = dict.fromkeys(LEVERS, "shadow")

    async def control() -> Sim:
        sim = Sim(s, path / "control", controls, HOT_WATER, record=True)
        async with running(sim):
            await sim.run(days, stand_in)
        return sim

    async def shadow(frames: list[Frame]) -> Sim:
        sim = Sim(s, path / "shadow", shadows, HOT_WATER, replay=frames)
        async with running(sim):
            await sim.run(days, stand_in)
        return sim

    (path / "control").mkdir()
    (path / "shadow").mkdir()
    done = run(s, control)
    copied = run(s, lambda: shadow(done.frames))
    wanted = intended(done)
    assert len(wanted) >= 8 * days
    assert {lever for _, lever, _, _ in wanted} >= set(LEVERS) - {"dhw/boost_once"}
    assert intended(copied) == wanted
    assert all(r.outcome == "shadowed" for r in copied.requests if r.intended)
    assert copied.bus.writes == []
    # Writes stay inside the claims: only the levers' own registers, never one that is
    # never written, never an alarm reset.
    model = load("bus").model("F1245")
    specs = {x.path: x for x in profile.levers(model, profile.BUS.layout(model, [1]).points, {})}
    claimed = {
        int(touched.removeprefix("x.nibe."))
        for lever in LEVERS
        for touched in specs[f"hp1/{lever}"].lever.touches
    }
    written = {register for _, register, _ in done.bus.writes}
    assert written <= claimed
    assert not written & (profile.NEVER_WRITTEN | {profile.ALARM_RESET})


def test_shadow_decides_as_control_and_writes_nothing(tmp_path: Path) -> None:
    shadow_equals_control(tmp_path, scenario(1), days=1)


@pytest.mark.nightly
@pytest.mark.parametrize("seed", range(2, 10))
def test_shadow_decides_as_control_over_days(tmp_path: Path, seed: int) -> None:
    emitter: Emitter = "floor" if seed % 2 else "radiators"
    s = draw(seed, emitter=emitter, start=datetime(2026, 1, 10 + seed, tzinfo=UTC))
    shadow_equals_control(tmp_path, s, days=3)
