"""The Nibe plugin for an S-series pump, over Modbus TCP against the simulated pump:
identification by the user's model, values and their quality, registers the pump hasn't
got, one value per request, and nothing written."""

import asyncio
import contextlib
import itertools
from collections.abc import AsyncIterator, Callable
from typing import Any

import pydantic
import pytest
from simspump import SimSPump

from thermaestro.cap import Link, pair, serve
from thermaestro.cap.conformance import run
from thermaestro.cap.messages import Described
from thermaestro.nibe import profile, sprofile
from thermaestro.nibe.maps import load
from thermaestro.nibe.plugin import IDENTIFICATION, NibePlugin
from thermaestro.store import NibeGateway

MODEL = "S1255"

PUMP = {
    30001: 0xFFC4,  # BT1 -6.0 °C
    30005: 351,  # BT2 35.1
    30007: 300,  # BT3 30.0
    30008: 510,  # BT7 51.0
    30009: 480,  # BT6 48.0
    30010: 20,  # BT10 2.0
    30011: 0xFFF6,  # BT11 -1.0
    30026: 0x8000,  # room sensor: not connected
    31028: 30,  # priority: heating
    31100: 1,  # compressor on
    31102: 50,  # GP1 50 %
    31104: 40,  # GP2 40 %
    32196: 0,  # QN10: heating
    31046: 452,  # 45.2 Hz
    31975: 0,
    40190: 0xFFB0,  # low brine-out alarm limit -8.0
    40030: 0xFFFC,  # offset -4
}


def stock(pump: SimSPump) -> None:
    """Every register the profile reads, as an S1255 without BT25 or BT5 would answer."""
    model = load("s-series").model(MODEL)
    layout = sprofile.S_SERIES.layout(model, [1])
    for definition in layout.points.values():
        size = model.register(definition.register).size
        if size is not None and size.bits == 32:
            pump.set32(definition.register, 0)
        else:
            pump.registers[definition.register] = 0
    for register in sprofile.S_SERIES.watched:
        pump.registers.setdefault(register, 0)
    pump.registers.update(PUMP)
    pump.set32(40011, -224)  # degree minutes -22.4
    pump.set32(31583, 12_345)  # hot water, compressor only: 1234.5 kWh
    pump.set32(31581, 0xFFFF_FFFF)  # a meter the pump doesn't keep
    for refused in (30039, 32014):  # BT25 and BT5: not installed
        del pump.registers[refused]


@pytest.fixture
async def spump() -> AsyncIterator[SimSPump]:
    pump = SimSPump()
    stock(pump)
    await pump.start()
    yield pump
    await pump.stop()


def settings(pump: SimSPump, model: str = MODEL, **kw: Any) -> NibeGateway:
    return NibeGateway(
        host="127.0.0.1", protocol="modbus-tcp", modbus_port=pump.port, model=model, **kw
    )


def made(pump: SimSPump, **kw: Any) -> NibePlugin:
    return NibePlugin(
        settings(pump, **kw), identify_timeout_s=5, health_interval_s=0.5, poll_round_s=0.2
    )


@pytest.fixture
async def plugin(spump: SimSPump) -> AsyncIterator[NibePlugin]:
    async with running_plugin(made(spump)) as running:
        yield running


@contextlib.asynccontextmanager
async def running_plugin(
    p: NibePlugin, events: list[object] | None = None
) -> AsyncIterator[NibePlugin]:
    core, plugin_side = pair()
    served = asyncio.create_task(serve(plugin_side, p))
    link = Link(core, on_event=events.append if events is not None else None)
    link.start()
    try:
        await link.hello(timeout=5)
        await link.describe(timeout=15)
        yield p
    finally:
        await link.close()
        await plugin_side.close()
        served.cancel()
        await asyncio.gather(served, return_exceptions=True)


async def until(condition: Callable[[], bool], timeout: float = 10.0) -> None:
    async with asyncio.timeout(timeout):
        while not condition():
            await asyncio.sleep(0.05)


def value(p: NibePlugin, point: str) -> tuple[object, str, str | None]:
    e = p.envelope(f"hp1/{point}")
    return e.value, e.quality, e.why


def all_read(p: NibePlugin) -> bool:
    _, layout = p._pump()
    return all(value(p, path)[2] != "not read yet" for path in layout.points)


async def test_it_identifies_the_pump_by_the_model_set(plugin: NibePlugin) -> None:
    assert plugin.family is sprofile.S_SERIES
    assert plugin.model is not None
    assert plugin.model.name == MODEL
    assert plugin.firmware is None
    assert plugin.high_word_first is True
    described = plugin.describe()
    unit = described.nodes[0]
    assert unit.identity is not None
    assert (unit.identity.model, unit.identity.map) == (MODEL, "nibe-s-series-S1255")
    assert {n.path for n in described.nodes} == {
        "hp1",
        "hp1/cs1",
        "hp1/dhw",
        "hp1/compressor.ep14",
        "hp1/brine",
    }
    # Described, and none usable until tried on a real S-series pump.
    assert {(lv.path, lv.unavailable) for lv in described.levers} == {
        ("hp1/cs1/heating.offset", "not yet read on a real S-series pump"),
        ("hp1/dhw/mode", "not yet read on a real S-series pump"),
    }
    assert value(plugin, IDENTIFICATION) == (
        None,
        "unknown",
        "the pump doesn't answer Modbus device identification",
    )


async def test_what_the_pump_says_of_itself_is_shown(spump: SimSPump) -> None:
    spump.identification = {0: b"NIBE", 1: b"S1255-6 PC EM 3x400V"}
    async with running_plugin(made(spump)) as p:
        assert p.identification == {"vendor": "NIBE", "product": "S1255-6 PC EM 3x400V"}
        assert value(p, IDENTIFICATION)[0] == "vendor: NIBE, product: S1255-6 PC EM 3x400V"
        point = next(x for x in p.describe().points if x.path == f"hp1/{IDENTIFICATION}")
        assert point.category == "diagnostic"
        # Shown, not relied on: the model is the one set.
        assert p.describe().nodes[0].label == MODEL


async def test_values_and_their_quality(plugin: NibePlugin) -> None:
    await until(lambda: all_read(plugin))
    assert value(plugin, "outdoor.temp") == (-6.0, "good", None)
    assert value(plugin, "cs1/supply.temp") == (35.1, "good", None)
    assert value(plugin, "degree_minutes") == (-22.4, "good", None)
    assert value(plugin, "demand") == ("heating", "good", None)
    assert value(plugin, "diverter") == ("heating", "good", None)
    assert value(plugin, "compressor.ep14/state") == ("running", "good", None)
    assert value(plugin, "compressor.ep14/speed") == (45.2, "good", None)
    assert value(plugin, "cs1/room.temp") == (None, "not_connected", "the pump reports no sensor")
    assert value(plugin, "heat.produced{purpose=dhw,by=compressor}") == (1234.5, "good", None)
    assert value(plugin, "brine/brine.delta_t") == (3.0, "good", None)
    assert value(plugin, "cs1/x.nibe.40030") == (-4, "good", None)
    assert plugin.envelope("hp1/compressor.ep14/speed").unit == "Hz"


async def test_the_valves_position_is_measured(plugin: NibePlugin, spump: SimSPump) -> None:
    await until(lambda: all_read(plugin))
    spump.registers[31028] = 20  # priority: hot water
    spump.registers[32196] = 1  # QN10: hot water
    await until(lambda: value(plugin, "diverter")[0] == "dhw")
    assert value(plugin, "demand")[0] == "dhw"
    assert value(plugin, "cs1/supply.temp") == (35.1, "no_flow", profile.DIVERTED)
    assert value(plugin, "cs1/return.temp")[1:] == ("no_flow", profile.DIVERTED)
    await until(lambda: value(plugin, "dhw/temp.charge")[1] == "transitional")


async def test_flow_rules(plugin: NibePlugin, spump: SimSPump) -> None:
    spump.registers[31102] = 0  # GP1 stopped
    spump.registers[31104] = 0  # GP2 stopped
    await until(lambda: value(plugin, "cs1/supply.temp")[1] == "no_flow")
    assert value(plugin, "cs1/supply.temp")[2] == profile.SUPPLY_STOPPED
    await until(lambda: value(plugin, "brine/brine.in.temp")[1] == "no_flow")
    assert value(plugin, "brine/brine.delta_t")[1] == "no_flow"


async def test_registers_the_pump_hasnt_got_are_left_out(spump: SimSPump) -> None:
    events: list[object] = []
    async with running_plugin(made(spump), events) as p:
        gone = {"hp1/cs1/x.nibe.30039", "hp1/dhw/x.nibe.32014"}
        assert gone <= {x.path for x in p.describe().points}
        await until(lambda: {30039, 32014, 31581} <= p.absent)
        points = {x.path for x in p.describe().points}
        assert not gone & points
        assert "hp1/heat.produced{purpose=pool,by=compressor}" not in points
        removed = {r for e in events if isinstance(e, Described) for r in e.removed}
        assert gone <= removed
        # Not asked for again.
        asked = len([r for r in spump.requests if r.address == 39 - 1])
        await asyncio.sleep(0.6)
        assert len([r for r in spump.requests if r.address == 39 - 1]) == asked


async def test_one_value_per_request_and_rounds_spaced(plugin: NibePlugin, spump: SimSPump) -> None:
    await until(lambda: all_read(plugin))
    wide = {
        r
        for r in load("s-series").model(MODEL)
        if (size := load("s-series").model(MODEL).register(r).size) is not None and size.bits == 32
    }
    for r in spump.requests:
        if r.function in (3, 4):
            number = (30_000 if r.function == 4 else 40_000) + r.address + 1
            assert r.count == (2 if number in wide else 1)
    await until(lambda: len([r for r in spump.requests if r.function == 4 and r.address == 0]) >= 3)
    outdoor = [r.t for r in spump.requests if r.function == 4 and r.address == 0]
    assert all(b - a >= 0.19 for a, b in itertools.pairwise(outdoor))
    assert {r.unit for r in spump.requests} == {1}


async def test_it_conforms_and_writes_nothing(spump: SimSPump) -> None:
    p = made(spump)
    core, plugin_side = pair()
    served = asyncio.create_task(serve(plugin_side, p))
    try:
        assert list(await run(core, timeout_s=30, quiet_s=0.3)) == []
    finally:
        await plugin_side.close()
        served.cancel()
        await asyncio.gather(served, return_exceptions=True)
    assert spump.writes == []


async def test_a_model_without_a_table_is_reported(spump: SimSPump) -> None:
    p = NibePlugin(settings(spump, model="S9999"), identify_timeout_s=2)
    core, plugin_side = pair()
    served = asyncio.create_task(serve(plugin_side, p))
    link = Link(core)
    link.start()
    try:
        await link.hello(timeout=5)
        await until(lambda: p.problem is not None)
        assert "S9999" in str(p.problem)
    finally:
        await link.close()
        await plugin_side.close()
        served.cancel()
        await asyncio.gather(served, return_exceptions=True)


def test_modbus_needs_the_model() -> None:
    with pytest.raises(pydantic.ValidationError, match="needs its model"):
        NibeGateway(host="pump.local", protocol="modbus-tcp")
    assert NibeGateway(host="pump.local", protocol="modbus-tcp", model="S1155").modbus_port == 502
