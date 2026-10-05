"""A simulated pump with the Python gateway in front of it, on localhost."""

from collections.abc import AsyncIterator

import pytest
from simpump import TEST_PSK, PtyEnd, SimPump
from thermaestro_gateway.server import Config, Gateway

REGISTERS = {
    40004: 0xFFC4,  # BT1 outdoor, -6.0 °C
    47011: 0xFFFC,  # heating offset, -4
    47134: 30,
    47135: 30,
    47387: 1,
}


@pytest.fixture
async def pump() -> AsyncIterator[SimPump]:
    end = PtyEnd()
    pump = SimPump(end, registers=dict(REGISTERS))
    yield pump
    await pump.stop()
    end.close()


async def _gateway(pump: SimPump, psk: bytes | None) -> Gateway:
    gateway = Gateway(
        Config(
            serial_port=pump.end.path,
            listen="127.0.0.1",
            read_port=0,
            write_port=0,
            control_port=0,
            psk=psk,
        )
    )
    await gateway.start()
    pump.start()
    return gateway


@pytest.fixture
async def gateway(pump: SimPump) -> AsyncIterator[Gateway]:
    gateway = await _gateway(pump, None)
    yield gateway
    await pump.stop()
    await gateway.close()


@pytest.fixture
async def gateway_with_psk(pump: SimPump) -> AsyncIterator[Gateway]:
    gateway = await _gateway(pump, TEST_PSK)
    yield gateway
    await pump.stop()
    await gateway.close()
