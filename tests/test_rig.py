"""`thermaestro rig` against the plant, in simulated time: each check through the executor,
read-only unless asked, everything put back on every way out, and a run that was killed
put back by `restore`. And the command, in real time, against the simulated pump."""

import asyncio
import inspect
import json
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import pytest
from plant import Draw, Plant, Scenario, begin, draw
from plant.bus import SimBus
from plant.model import STEP_S, s16, signed
from plant.scenario import Pool
from plant.vloop import every, simulate
from simpump import SimPump
from test_nibe_plugin import PUMP
from thermaestro_gateway.server import Gateway

from thermaestro import clock
from thermaestro.cli import main
from thermaestro.nibe.rig import CHECKS, Options, Report, RigError, run
from thermaestro.nibe.rig import bench as bench_module
from thermaestro.nibe.rig import checks as checks_module
from thermaestro.nibe.rig.bench import Bench
from thermaestro.store import NibeGateway

PLANT_PUMP = NibeGateway(host="plant.invalid", model="F1245")
PLUGIN = {"poll_round_s": 300.0, "health_interval_s": 60.0}


def scenario(seed: int = 1, **changes: Any) -> Scenario:
    return replace(draw(seed, emitter="radiators"), **changes)


@dataclass
class Person:
    """Answers the rig's questions: yes, unless told otherwise for a question."""

    answers: dict[str, bool | None] = field(default_factory=dict)
    asked: list[str] = field(default_factory=list)

    async def __call__(self, question: str) -> bool | None:
        self.asked.append(question)
        return next((a for q, a in self.answers.items() if q in question), True)


@dataclass
class Rigged:
    report: Report
    plant: Plant
    bus: SimBus
    said: list[str]
    person: Person

    def outcomes(self) -> list[tuple[str, str]]:
        return [(s.what, s.outcome) for s in self.report.steps]

    def step(self, start: str) -> Any:
        return next(s for s in self.report.steps if s.what.startswith(start))


def rig(
    tmp_path: Path,
    s: Scenario,
    check: str,
    *,
    write: bool = True,
    options: Options | None = None,
    person: Person | None = None,
    prepare: Callable[[Plant], None] | None = None,
    during: Callable[[Plant, SimBus, asyncio.Event], Coroutine[Any, Any, None]] | None = None,
) -> Rigged:
    """Run a check against a plant in simulated time; `during` runs beside it."""
    plant = begin(s)
    if prepare is not None:
        prepare(plant)
    bus = SimBus(plant)
    said: list[str] = []
    person = person or Person()

    async def body() -> Report:
        stepping = asyncio.create_task(every(STEP_S, lambda: plant.run(clock.time() - plant.t)))
        interrupt = asyncio.Event()
        beside: asyncio.Task[None] | None = None
        if during is not None:
            beside = asyncio.create_task(during(plant, bus, interrupt))
        try:
            return await run(
                check,
                PLANT_PUMP,
                state=tmp_path / "rig-state",
                write=write,
                options=options or Options(),
                say=said.append,
                ask=person,
                connect_fn=bus.connect,
                identify_timeout_s=120.0,
                plugin_options=PLUGIN,
                interrupt=interrupt,
            )
        finally:
            for task in (stepping, beside):
                if task is not None:
                    task.cancel()
            await asyncio.gather(stepping, *([beside] if beside else []), return_exceptions=True)
            await bus.close()

    report = simulate(body, s.start_time)
    return Rigged(report, plant, bus, said, person)


def written(bus: SimBus) -> list[tuple[int, int]]:
    return [(register, value) for _, register, value in bus.writes]


def test_offset(tmp_path: Path) -> None:
    s = scenario()
    found = signed(begin(s).registers[47011], 8)
    r = rig(tmp_path, s, "offset")
    assert r.outcomes()[-2:] == [
        (f"set cs1/heating.offset to {found + 1}", "verified"),
        ("put back cs1/heating.offset", "verified"),
    ]
    sent = [(register, signed(value & 0xFF, 8)) for register, value in written(r.bus)]
    assert sent == [(47011, found + 1), (47011, found)]
    assert r.report.evidence is not None
    assert r.report.evidence["levers"] == ["hp1/cs1/heating.offset"]
    assert "Go ahead?" in r.person.asked
    assert not r.report.failed
    assert "1  " in r.report.table()


def test_read_only_writes_nothing_and_says_what_it_would(tmp_path: Path) -> None:
    s = scenario()
    r = rig(tmp_path, s, "offset", write=False)
    assert r.bus.writes == []
    assert [o for _, o in r.outcomes()][-2:] == ["shadowed", "shadowed"]
    assert "Go ahead?" not in r.person.asked
    assert r.report.evidence is None
    assert not r.report.wrote
    assert any("It would write" in line for line in r.said)
    # A second run takes the lever over afresh: nothing is left of the first.
    again = rig(tmp_path, s, "offset", write=False)
    assert again.bus.writes == []


def test_declining_writes_nothing(tmp_path: Path) -> None:
    r = rig(tmp_path, scenario(), "offset", person=Person({"Go ahead?": False}))
    assert r.bus.writes == []
    assert r.outcomes()[-1] == ("go ahead", "declined: nothing was written")


def test_a_competing_feature_not_confirmed_off_refuses_the_lever(tmp_path: Path) -> None:
    r = rig(tmp_path, scenario(), "mode", person=Person({"hot-water schedule": False}))
    assert r.bus.writes == []
    refused = r.step("set dhw/mode")
    assert refused.outcome == "refused"
    assert "may be on" in refused.detail
    assert r.report.failed


def test_mode(tmp_path: Path) -> None:
    r = rig(tmp_path, scenario(), "mode")
    assert written(r.bus) == [(47041, 0), (47041, 1)]  # normal to eco and back
    assert r.step("set dhw/mode to eco").outcome == "verified"
    assert r.step("the start temperature now").what.startswith("the start temperature now: 42 °C")
    assert r.step("put back dhw/mode").outcome == "verified"
    assert not r.report.failed


def test_block(tmp_path: Path) -> None:
    """A draw at 01:00 takes the charge sensor below the start: the block holds the charge
    off, puts the start back, and the pump charges then."""
    s = scenario(draws=(Draw(hour=1.0, liters=60.0, minutes=8.0),)).set({47050: 0})
    r = rig(tmp_path, s, "block", options=Options(minutes=6 * 60))
    held = r.step("held off")
    assert held.passed is True
    assert r.step("the start temperature (47044) reads 25.0").passed is True
    assert r.step("the start temperature (47044) is back at 45").passed is True
    assert r.step("a charge started").what.endswith("after the release")
    assert r.report.judged is True
    assert r.plant.registers[47044] == s16(45.0)
    assert written(r.bus) == [(47044, s16(25.0)), (47044, s16(45.0))]
    assert r.report.evidence is not None
    assert r.report.timeline
    assert "charge sensor" in r.report.timeline[0]


def test_block_with_nothing_to_hold_off(tmp_path: Path) -> None:
    s = scenario().set({47050: 0})
    r = rig(tmp_path, s, "block", options=Options(minutes=30))
    assert r.step("the charge sensor stayed at or above").outcome == "not seen"
    assert r.plant.registers[47044] == s16(45.0)
    assert not r.report.failed
    assert r.report.evidence is not None  # the write and the put-back went as they should


def test_boost(tmp_path: Path) -> None:
    s = scenario().set({47050: 0})
    r = rig(tmp_path, s, "boost")
    assert r.step("fire dhw/boost_once").outcome == "awaiting_effect"
    assert r.step("a charge started").passed is True
    assert r.step("the charge ended").passed is True
    assert written(r.bus) == [(48132, 4)]
    assert r.report.judged is True


def cold(s: Scenario) -> Scenario:
    """A pump too small for its house in the cold: the addition runs."""
    return replace(s, heat_kw=1.5, weather=replace(s.weather, mean_c=-15.0, swing_c=1.0))


def test_addition(tmp_path: Path) -> None:
    def deep(plant: Plant) -> None:
        plant.dm = -900.0

    r = rig(tmp_path, cold(scenario()).set({47050: 0}), "addition", prepare=deep)
    assert r.step("set addition/stop_temp to -25").outcome == "verified"
    assert r.step("the addition stopped").passed is True
    assert r.step("put back addition/stop_temp").outcome == "verified"
    assert r.plant.registers[47376] == s16(5.0)
    assert not r.report.failed


def test_addition_max_power(tmp_path: Path) -> None:
    def deep(plant: Plant) -> None:
        plant.dm = -900.0

    options = Options(setting="max_power")
    r = rig(tmp_path, cold(scenario()).set({47050: 0}), "addition", options=options, prepare=deep)
    assert r.step("set addition/max_power to 0").outcome == "verified"
    assert r.step("the addition stopped").passed is True
    assert r.plant.registers[47212] == 650


def test_addition_that_doesnt_run(tmp_path: Path) -> None:
    s = scenario(weather=replace(scenario().weather, mean_c=10.0)).set({47050: 0})
    r = rig(tmp_path, s, "addition", options=Options(minutes=20))
    assert r.step("the addition didn't run").outcome == "not seen"
    assert r.bus.writes == []


def test_pool(tmp_path: Path) -> None:
    s = scenario(pool=Pool(start_c=20.0)).set({47050: 0})
    r = rig(tmp_path, s, "pool")
    assert r.step("set pool1/start_temp to 21").outcome == "verified"
    assert r.step("set pool1/stop_temp to 29").outcome == "verified"
    assert r.step("48094 reads 0").passed is True
    assert r.step("48094 is back at").passed is True
    for register, word in ((48090, 220), (48092, 280), (48094, 1)):
        assert r.plant.registers[register] == word
    assert not r.report.failed


def test_a_lever_the_pump_lacks(tmp_path: Path) -> None:
    with pytest.raises(RigError, match="this pump has no pool1/start_temp"):
        rig(tmp_path, scenario(), "pool")


def test_stopped_by_the_person_puts_back(tmp_path: Path) -> None:
    s = scenario().set({47050: 0})

    async def stop(plant: Plant, bus: SimBus, interrupt: asyncio.Event) -> None:
        while plant.registers[47044] != s16(25.0):
            await asyncio.sleep(10)
        await asyncio.sleep(1800)
        interrupt.set()

    r = rig(tmp_path, s, "block", during=stop)
    assert ("stopped", "stopped by the person") in r.outcomes()
    assert r.step("put back dhw/block").passed is True
    assert r.plant.registers[47044] == s16(45.0)


def test_another_clients_change_is_let_go_not_written_over(tmp_path: Path) -> None:
    """NibePi beside the rig sets the start while the block holds: the rig lets the lever
    go, says so, and doesn't write over it."""
    s = scenario().set({47050: 0})

    async def nibepi(plant: Plant, bus: SimBus, interrupt: asyncio.Event) -> None:
        while plant.registers[47044] != s16(25.0):
            await asyncio.sleep(10)
        await asyncio.sleep(600)
        bus.foreign_write(47044, s16(40.0))

    r = rig(tmp_path, s, "block", options=Options(minutes=60), during=nibepi)
    let_go = r.step("dhw/block was changed by something else")
    assert let_go.outcome == "let go"
    assert "47044" in let_go.detail
    assert r.report.failed
    assert r.plant.registers[47044] == s16(40.0)
    assert written(r.bus) == [(47044, s16(25.0)), (47044, s16(40.0))]  # the rig's, NibePi's


def test_a_killed_run_is_put_back_by_restore(tmp_path: Path) -> None:
    """A run that dies with the block engaged: no check starts until `restore` has put it
    back, and restore writes only with --write."""
    s = scenario().set({47050: 0})
    plant = begin(s)
    bus = SimBus(plant)

    async def dies() -> None:
        bench = Bench(
            PLANT_PUMP,
            state=tmp_path / "rig-state",
            write=True,
            say=lambda _: None,
            ask=Person(),
            connect_fn=bus.connect,
            identify_timeout_s=120.0,
            plugin_options=PLUGIN,
        )
        await bench.open()
        await bench.ready(["dhw/block"], [])
        await bench.consent([], ["dhw/block"])
        await bench.act("dhw/block", "engage", why="a run that dies")
        # Gone, without putting anything back.
        await bench.executor.stop()
        assert bench._host is not None
        assert bench._tmp is not None
        await bench._host.stop()
        await bench.db.close()
        bench._tmp.cleanup()

    simulate(dies, s.start_time)
    assert plant.registers[47044] == s16(25.0)

    def again(check: str, write: bool) -> Rigged:
        return rig(
            tmp_path, s, check, write=write, prepare=lambda p: p.registers.update(plant.registers)
        )

    with pytest.raises(RigError, match="put it back first"):
        again("block", True)
    shown = again("restore", False)
    assert shown.outcomes()[-1] == ("would put back pump:hp1/dhw/block", "shadowed")
    assert shown.bus.writes == []
    restored = again("restore", True)
    assert restored.step("put back dhw/block").passed is True
    assert restored.plant.registers[47044] == s16(45.0)
    assert again("restore", True).outcomes()[-1] == (
        "anything left changed by an earlier run",
        "nothing",
    )


def test_the_rig_writes_only_through_the_executor() -> None:
    for module in (bench_module, checks_module):
        source = inspect.getsource(module)
        assert ".write(" not in source
        assert "link.act" not in source


def test_every_check_is_listed() -> None:
    assert set(CHECKS) == {"offset", "mode", "block", "boost", "addition", "pool"}


async def test_the_command(
    pump: SimPump, gateway: Gateway, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pump.registers.update(PUMP)
    pump.registers[47394] = 0  # the pump's room control off
    pump.info_interval_s = 0.2
    monkeypatch.setattr("builtins.input", lambda prompt: "y")
    argv = [
        "rig",
        "offset",
        "127.0.0.1",
        "--read-port",
        str(gateway.ports["read"]),
        "--write-port",
        str(gateway.ports["write"]),
        "--out",
        str(tmp_path),
    ]
    assert await asyncio.to_thread(main, argv) == 0  # read-only
    assert pump.taken_writes == []
    assert await asyncio.to_thread(main, [*argv, "--write"]) == 0
    assert [(r, signed(v & 0xFF, 8)) for r, v in pump.taken_writes] == [(47011, -3), (47011, -4)]
    logs = sorted(tmp_path.glob("rig-offset-F1245-*.json"))
    assert logs
    log = json.loads(logs[-1].read_text())
    assert log["format"] == "thermaestro-rig"
    assert log["wrote"] is True
    assert log["evidence"]["firmware"] == "9721R4"
    assert "127.0.0.1" not in logs[-1].read_text()
