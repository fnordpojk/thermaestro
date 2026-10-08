"""The Modbus TCP transport against the simulated S-series pump: register numbering, the
order of a 32-bit value's words, one value per request, the pace, and no writes."""

from collections.abc import AsyncIterator

import pytest
from simspump import SimSPump

from thermaestro.nibe.maps import decode, load, words
from thermaestro.nibe.transport import ModbusConfig, connect
from thermaestro.nibe.transport.base import Observed, ReadFailed, RegisterRefused, WriteResult
from thermaestro.nibe.transport.modbus import PER_SECOND, ModbusTransport


@pytest.fixture
async def spump() -> AsyncIterator[SimSPump]:
    pump = SimSPump(
        registers={
            30001: 0xFFC4,  # BT1 -6.0 °C
            30005: 351,  # BT2 35.1
            31028: 30,  # priority: heating
            40030: 0xFFFC,  # heating offset -4
        }
    )
    pump.set32(40011, -224)  # degree minutes -22.4
    pump.set32(31583, 12_345)  # hot water, compressor only: 1234.5 kWh
    await pump.start()
    yield pump
    await pump.stop()


@pytest.fixture
async def transport(spump: SimSPump) -> AsyncIterator[ModbusTransport]:
    t = ModbusTransport("127.0.0.1", spump.port, wide={40011, 31583}, timeout_s=1.0)
    await t.start()
    yield t
    await t.close()


async def test_input_and_holding_registers(transport: ModbusTransport, spump: SimSPump) -> None:
    outdoor = await transport.read(30001)
    offset = await transport.read(40030)
    assert words(outdoor.data) == (0xFFC4, 0)
    assert words(offset.data) == (0xFFFC, 0)
    # Function 4 for input registers and 3 for holding ones, at address n - 1, unit 1.
    assert [(r.function, r.address, r.count, r.unit) for r in spump.requests] == [
        (4, 0, 1, 1),
        (3, 29, 1, 1),
    ]


async def test_a_32_bit_value_comes_low_word_first(
    transport: ModbusTransport, spump: SimSPump
) -> None:
    table = load("s-series").model("S1255")
    for register, expected in ((40011, -22.4), (31583, 1234.5)):
        reading = await transport.read(register)
        first, second = words(reading.data)
        decoded = decode(table.register(register), first, second, high_word_first=True)
        assert decoded.value == expected
    assert [r.count for r in spump.requests] == [2, 2]


async def test_one_value_per_request(transport: ModbusTransport, spump: SimSPump) -> None:
    for register in (30001, 30005, 31028, 40030, 40011):
        await transport.read(register)
    assert [r.count for r in spump.requests] == [1, 1, 1, 1, 2]
    assert max(r.count for r in spump.requests) <= 20


async def test_the_pace_stays_within_100_registers_a_second(
    transport: ModbusTransport, spump: SimSPump
) -> None:
    for _ in range(30):
        await transport.read(30001)
    times = [r.t for r in spump.requests]
    # 30 registers can't take less than 29 intervals of 1/100 s.
    assert times[-1] - times[0] >= 29 / PER_SECOND * 0.95


async def test_a_refused_read_never_becomes_a_value(transport: ModbusTransport) -> None:
    with pytest.raises(RegisterRefused, match="hasn't got it"):
        await transport.read(30002)  # the simulated pump hasn't got it
    for outside in (20001, 50001, 30000):
        with pytest.raises(ReadFailed, match="not an S-series register"):
            await transport.read(outside)
    assert transport.health().detail == {"reads": 1, "failed": 1}


async def test_a_silent_pump(spump: SimSPump) -> None:
    spump.silent = True
    t = ModbusTransport("127.0.0.1", spump.port, timeout_s=0.3)
    await t.start()
    try:
        with pytest.raises(ReadFailed, match="no answer"):
            await t.read(30001, timeout=0.5)
    finally:
        await t.close()


async def test_requests_and_answers_are_observed(transport: ModbusTransport) -> None:
    seen: list[Observed] = []
    stop = transport.observe(seen.append)
    await transport.read(30001)
    stop()
    await transport.read(30001)
    assert len(seen) == 1
    assert seen[0].telegram is None
    assert seen[0].reply[7:] == bytes((4, 0, 0, 0, 1))  # what was sent: function 4, 1 register
    assert seen[0].data[7:] == bytes((4, 2, 0xFF, 0xC4))  # what came back
    assert transport.health().up
    assert transport.health().last_traffic is not None


async def test_device_identification_where_answered(
    transport: ModbusTransport, spump: SimSPump
) -> None:
    assert await transport.identify() is None
    spump.identification = {0: b"NIBE", 1: b"S1255-6", 2: b"1.2.3"}
    assert await transport.identify() == {
        "vendor": "NIBE",
        "product": "S1255-6",
        "revision": "1.2.3",
    }


async def test_nothing_is_written(transport: ModbusTransport, spump: SimSPump) -> None:
    outcome = await transport.write(40030, 2)
    assert outcome.result is WriteResult.NOT_TAKEN
    assert spump.writes == []


async def test_connect_picks_modbus(spump: SimSPump) -> None:
    t = await connect(ModbusConfig("127.0.0.1", spump.port, wide=frozenset({40011})))
    try:
        assert isinstance(t, ModbusTransport)
        assert t.health().protocol == "modbus-tcp"
        assert words((await t.read(40011)).data) == (0xFFFF, 0xFF20)
    finally:
        await t.close()


async def test_a_pump_that_isnt_there() -> None:
    t = ModbusTransport("127.0.0.1", 1, timeout_s=0.3)
    with pytest.raises(OSError, match="doesn't answer"):
        await t.start()
    await t.close()
