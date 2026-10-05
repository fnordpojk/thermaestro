"""The whole gateway, with a simulated pump on a pseudo-terminal and UDP clients on localhost."""

import asyncio
import os
import tty
from collections.abc import AsyncIterator

import pytest
from thermaestro_gateway import nibe
from thermaestro_gateway import protocol as p
from thermaestro_gateway.server import Config, Gateway

READ_TOKEN = bytes.fromhex("5c0020690049")
TIMEOUT_S = 2.0


class Pump:
    """The pump's end of a pseudo-terminal; the gateway opens the other end."""

    def __init__(self) -> None:
        self.master, self._slave = os.openpty()
        tty.setraw(self.master)
        self.path = os.ttyname(self._slave)
        self._received = bytearray()
        self._arrived = asyncio.Event()
        asyncio.get_running_loop().add_reader(self.master, self._readable)

    def _readable(self) -> None:
        self._received += os.read(self.master, 1024)
        self._arrived.set()

    def send(self, data: bytes) -> None:
        os.write(self.master, data)

    async def expect(self, n: int) -> bytes:
        async with asyncio.timeout(TIMEOUT_S):
            while len(self._received) < n:
                self._arrived.clear()
                await self._arrived.wait()
        data, self._received = bytes(self._received[:n]), self._received[n:]
        return data

    def close(self) -> None:
        asyncio.get_running_loop().remove_reader(self.master)
        os.close(self.master)
        os.close(self._slave)


class Client(asyncio.DatagramProtocol):
    def __init__(self) -> None:
        self.inbox: asyncio.Queue[bytes] = asyncio.Queue()
        self.transport: asyncio.DatagramTransport | None = None

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        if isinstance(transport, asyncio.DatagramTransport):
            self.transport = transport

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        self.inbox.put_nowait(data)

    def send(self, data: bytes, port: int) -> None:
        assert self.transport is not None
        self.transport.sendto(data, ("127.0.0.1", port))

    async def receive(self) -> bytes:
        async with asyncio.timeout(TIMEOUT_S):
            return await self.inbox.get()

    async def message(self) -> p.Message:
        return p.decode(await self.receive()).message


@pytest.fixture
async def pump() -> AsyncIterator[Pump]:
    pump = Pump()
    yield pump
    pump.close()


@pytest.fixture
async def gateway(pump: Pump) -> AsyncIterator[Gateway]:
    gw = Gateway(
        Config(serial_port=pump.path, listen="127.0.0.1", read_port=0, write_port=0, control_port=0)
    )
    await gw.start()
    yield gw
    await gw.close()


@pytest.fixture
async def client() -> AsyncIterator[Client]:
    loop = asyncio.get_running_loop()
    transport, protocol = await loop.create_datagram_endpoint(Client, local_addr=("127.0.0.1", 0))
    yield protocol
    transport.close()


async def test_a_plain_read_through_the_gateway(
    pump: Pump, gateway: Gateway, client: Client
) -> None:
    request = nibe.read_request(47134)
    client.send(request, gateway.ports["read"])
    await asyncio.sleep(0.05)
    pump.send(READ_TOKEN)
    assert await pump.expect(len(request)) == request
    pump.send(b"\x06")
    assert await client.receive() == READ_TOKEN + request + b"\x06"


async def test_a_protocol_read_from_hello_to_answer(
    pump: Pump, gateway: Gateway, client: Client
) -> None:
    control = gateway.ports["control"]
    client.send(
        p.encode(
            p.Hello(id=1, options=(p.Option.u32(p.Tag.SUBSCRIBE, p.Subscription.FRAMES_OWN),))
        ),
        control,
    )
    assert isinstance(await client.message(), p.Welcome)

    request = nibe.read_request(47134)
    client.send(
        p.encode(
            p.Request(
                id=42,
                address=nibe.MODBUS40,
                token=nibe.READ_TOKEN,
                flags=p.RequestFlag.EXPECT_ANSWER,
                frame=request,
            )
        ),
        control,
    )
    queued = await client.message()
    assert isinstance(queued, p.Fate)
    assert (queued.id, queued.stage) == (42, p.Stage.QUEUED)

    pump.send(READ_TOKEN)
    assert await pump.expect(len(request)) == request
    pump.send(b"\x06")
    got = [await client.message() for _ in range(3)]
    stages = [m.stage for m in got if isinstance(m, p.Fate)]
    assert stages == [p.Stage.SENT, p.Stage.PUMP_ACK]
    (frame,) = [m for m in got if isinstance(m, p.Frame)]
    assert (frame.origin, frame.request_id) == (p.Origin.THIS_CLIENT, 42)

    payload = (47134).to_bytes(2, "little") + (45).to_bytes(2, "little") + b"\x00\x00"
    body = bytes((0x00, 0x20, nibe.READ_ANSWER, len(payload))) + payload
    answer_telegram = b"\x5c" + body + bytes((nibe.checksum(body),))
    pump.send(answer_telegram)
    assert await pump.expect(1) == b"\x06"
    answer = await client.message()
    assert isinstance(answer, p.Answer)
    assert (answer.id, answer.status, answer.frame) == (42, p.AnswerStatus.OK, answer_telegram)
