import asyncio
import socket
from collections.abc import AsyncIterator, Iterator

import pytest
from simpump import TEST_PSK, SimPump
from thermaestro_gateway import protocol as p
from thermaestro_gateway.server import Config, Gateway

from thermaestro.nibe.transport import GatewayConfig, connect
from thermaestro.nibe.transport.nibegw import PlainClient, PlainSettings
from thermaestro.nibe.transport.tgw import HandshakeFailed, HandshakeIssue, TgwClient, TgwSettings

TGW = TgwSettings(hello_wait_s=0.2, tick_s=0.05)
PLAIN = PlainSettings(resend_s=0.5, answer_s=0.5, tick_s=0.05)


@pytest.fixture
async def plain_only(pump: SimPump) -> AsyncIterator[Gateway]:
    """A gateway without the control port, like esphome-nibe upstream."""
    gateway = Gateway(
        Config(
            serial_port=pump.end.path,
            listen="127.0.0.1",
            read_port=0,
            write_port=0,
            control_port=None,
        )
    )
    await gateway.start()
    pump.start()
    yield gateway
    await pump.stop()
    await gateway.close()


@pytest.fixture
def silent_port() -> Iterator[int]:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.bind(("127.0.0.1", 0))
        yield s.getsockname()[1]


def config(gateway: Gateway, control_port: int | None, psk: bytes | None = None) -> GatewayConfig:
    return GatewayConfig(
        host="127.0.0.1",
        read_port=gateway.ports["read"],
        write_port=gateway.ports["write"],
        control_port=control_port,
        psk=psk,
    )


async def test_the_protocol_when_the_gateway_speaks_it(gateway: Gateway) -> None:
    transport = await connect(
        config(gateway, gateway.ports["control"]), tgw_settings=TGW, plain_settings=PLAIN
    )
    try:
        assert isinstance(transport, TgwClient)
        assert (await transport.read(47134, timeout=5)).data[:2] == b"\x1e\x00"
    finally:
        await transport.close()


async def test_plain_nibegw_when_the_control_port_is_silent(
    plain_only: Gateway, silent_port: int
) -> None:
    transport = await connect(
        config(plain_only, silent_port), tgw_settings=TGW, plain_settings=PLAIN
    )
    try:
        assert isinstance(transport, PlainClient)
        assert (await transport.read(47134, timeout=5)).data[:2] == b"\x1e\x00"
    finally:
        await transport.close()


async def test_never_plain_when_a_key_is_configured(plain_only: Gateway, silent_port: int) -> None:
    # Stop and report: the plain ports are unauthenticated (spec §13).
    with pytest.raises(HandshakeFailed) as failed:
        await connect(
            config(plain_only, silent_port, TEST_PSK), tgw_settings=TGW, plain_settings=PLAIN
        )
    assert failed.value.issue is HandshakeIssue.SILENT


async def test_plain_nibegw_when_the_gateway_speaks_another_version(plain_only: Gateway) -> None:
    loop = asyncio.get_running_loop()

    class NextVersion(asyncio.DatagramProtocol):
        def connection_made(self, transport: asyncio.BaseTransport) -> None:
            assert isinstance(transport, asyncio.DatagramTransport)
            self.transport = transport

        def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
            reply = bytearray(p.encode(p.Error(code=p.ErrorCode.BAD_VERSION)))
            reply[2] = p.VERSION_MAJOR + 1
            self.transport.sendto(bytes(reply), addr)

    endpoint, _ = await loop.create_datagram_endpoint(NextVersion, local_addr=("127.0.0.1", 0))
    try:
        port = endpoint.get_extra_info("sockname")[1]
        transport = await connect(config(plain_only, port), tgw_settings=TGW, plain_settings=PLAIN)
        assert isinstance(transport, PlainClient)
        await transport.close()
    finally:
        endpoint.close()


async def test_other_refusals_are_reported_not_hidden(gateway_with_psk: Gateway) -> None:
    with pytest.raises(HandshakeFailed) as failed:
        await connect(
            config(gateway_with_psk, gateway_with_psk.ports["control"]),
            tgw_settings=TGW,
            plain_settings=PLAIN,
        )
    assert failed.value.issue is HandshakeIssue.AUTH_REQUIRED


def test_a_key_needs_the_control_port() -> None:
    with pytest.raises(ValueError, match="control port"):
        GatewayConfig(host="127.0.0.1", control_port=None, psk=TEST_PSK)
