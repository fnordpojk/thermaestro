import asyncio
import socket
from collections.abc import AsyncIterator, Callable

import pytest
from simpump import LOG_SET, SimPump
from thermaestro_gateway import nibe
from thermaestro_gateway.protocol import Stage
from thermaestro_gateway.server import Gateway

from thermaestro.nibe.transport.base import FateKind, Observed, ReadFailed, WriteResult
from thermaestro.nibe.transport.nibegw import PlainClient, PlainSettings

FAST = PlainSettings(resend_s=0.5, answer_s=0.5, silent_s=1.0, tick_s=0.05)


@pytest.fixture
async def client(gateway: Gateway) -> AsyncIterator[PlainClient]:
    c = PlainClient("127.0.0.1", gateway.ports["read"], gateway.ports["write"], settings=FAST)
    await c.start()
    yield c
    await c.close()


def word(value: int) -> bytes:
    return value.to_bytes(2, "little")


async def test_a_read(client: PlainClient) -> None:
    r = await client.read(47134, timeout=5)
    assert (r.register, r.data) == (47134, word(30) + word(30))
    assert r.t_taken is not None
    assert r.t_taken < r.t_answered
    assert [s.stage for s in r.stages] == [Stage.SENT, Stage.PUMP_ACK]
    assert client.promises.fate is FateKind.BEST_EFFORT


async def test_a_write_is_accepted_and_reads_back(client: PlainClient, pump: SimPump) -> None:
    outcome = await client.write(47011, 0xFFFE, timeout=5)
    assert outcome.result is WriteResult.ACCEPTED
    assert outcome.t_result is not None
    back = await client.read(47011, after=outcome.t_result, timeout=5)
    assert back.data[:2] == word(0xFFFE)
    assert pump.taken_writes == [(47011, 0xFFFE)]


async def test_a_refused_write(client: PlainClient, pump: SimPump) -> None:
    pump.refuse.add(47011)
    assert (await client.write(47011, 1, timeout=5)).result is WriteResult.REFUSED


async def test_accepted_isnt_kept(client: PlainClient, pump: SimPump) -> None:
    pump.not_kept.add(47387)
    outcome = await client.write(47387, 0, timeout=5)
    assert outcome.result is WriteResult.ACCEPTED
    assert outcome.t_result is not None
    back = await client.read(47387, after=outcome.t_result, timeout=5)
    assert back.data[:2] == word(1)


async def test_a_read_back_waits_out_a_read_taken_before_the_write(
    client: PlainClient, pump: SimPump, gateway: Gateway
) -> None:
    # Another client's read of the register is taken just before the write, and its
    # answer, with the old value, comes after the write's 0x6C.
    pump.answer_delay_s = 0.4
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as other:
        other.sendto(nibe.read_request(47011), ("127.0.0.1", gateway.ports["read"]))
        outcome = await client.write(47011, 5, timeout=5)
        assert outcome.t_result is not None
        back = await client.read(47011, after=outcome.t_result, timeout=5)
    assert pump.taken_reads[0] == 47011
    assert back.data[:2] == word(5)


async def test_waiting_for_quiet_doesnt_use_up_a_read_backs_timeout(
    client: PlainClient, pump: SimPump
) -> None:
    # Read a moment ago, as before a write: the register isn't quiet for answer_s yet.
    first = await client.read(47011, timeout=5)
    back = await client.read(47011, after=first.t_answered, timeout=FAST.answer_s)
    assert back.data == first.data
    assert pump.taken_reads.count(47011) == 2


async def test_a_read_that_is_never_answered_fails(client: PlainClient, pump: SimPump) -> None:
    pump.silent_reads = 1000
    with pytest.raises(ReadFailed):
        await client.read(47134, timeout=1.5)


def first_observed(
    client: PlainClient, match: Callable[[Observed], bool]
) -> asyncio.Future[Observed]:
    found: asyncio.Future[Observed] = asyncio.get_running_loop().create_future()

    def check(o: Observed) -> None:
        if not found.done() and match(o):
            found.set_result(o)

    found.add_done_callback(lambda _: stop())
    stop = client.observe(check)
    return found


async def test_observers_see_the_log_set_stream(client: PlainClient, pump: SimPump) -> None:
    pump.log_set = [40004]
    pump.log_set_interval_s = 0.05
    log_set = first_observed(
        client, lambda o: o.telegram is not None and o.telegram.command == LOG_SET
    )
    await client.read(47134, timeout=5)  # makes this client a target
    telegram = (await asyncio.wait_for(log_set, 3)).telegram
    assert telegram is not None
    assert telegram.payload == word(40004) + word(0xFFC4)


async def test_it_stays_a_target_without_requests(gateway: Gateway, pump: SimPump) -> None:
    settings = PlainSettings(keepalive_s=0.2, tick_s=0.05)
    c = PlainClient("127.0.0.1", gateway.ports["read"], gateway.ports["write"], settings=settings)
    await c.start()
    try:
        await asyncio.wait_for(first_observed(c, lambda o: True), 3)
        assert c.health().up
    finally:
        await c.close()
