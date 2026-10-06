"""The simulated pump's behaviors, each as a transport client sees it, as the real pump showed
it on the bus."""

import asyncio
from collections.abc import AsyncIterator, Callable

import pytest
from simpump import MODBUS_ADDRESS, PRODUCT_INFO, WORD_SWAP, OtherClient, SimPump
from thermaestro_gateway import nibe
from thermaestro_gateway.server import Gateway

from thermaestro.nibe.maps import decode, load, words
from thermaestro.nibe.transport.base import Observed, ReadFailed, WriteResult
from thermaestro.nibe.transport.nibegw import PlainClient, PlainSettings
from thermaestro.nibe.transport.tgw import TgwClient, TgwSettings

FAST = PlainSettings(resend_s=0.5, answer_s=0.5, silent_s=1.0, tick_s=0.05)


@pytest.fixture
async def client(gateway: Gateway) -> AsyncIterator[PlainClient]:
    c = PlainClient("127.0.0.1", gateway.ports["read"], gateway.ports["write"], settings=FAST)
    await c.start()
    yield c
    await c.close()


@pytest.fixture
async def other(gateway: Gateway) -> AsyncIterator[OtherClient]:
    o = OtherClient("127.0.0.1", gateway.ports["read"], gateway.ports["write"])
    await o.start()
    yield o
    o.close()


def first_observed(
    client: PlainClient | TgwClient, match: Callable[[Observed], bool]
) -> asyncio.Future[Observed]:
    found: asyncio.Future[Observed] = asyncio.get_running_loop().create_future()

    def check(o: Observed) -> None:
        if not found.done() and match(o):
            found.set_result(o)

    found.add_done_callback(lambda _: stop())
    stop = client.observe(check)
    return found


@pytest.mark.parametrize(("swap", "data"), [(0, "0100f355"), (1, "f3550100")])
async def test_32_bit_answers_follow_48852(
    client: PlainClient, pump: SimPump, swap: int, data: str
) -> None:
    pump.registers32[43416] = 87_539
    pump.registers[WORD_SWAP] = swap
    r = await client.read(43416, timeout=5)
    assert r.data == bytes.fromhex(data)
    starts = load("bus").model("F1245").register(43416)
    assert decode(starts, *words(r.data), high_word_first=swap == 0).value == 87_539


@pytest.mark.parametrize(("swap", "data"), [(0, "ffffffff"), (1, "1eff1eff")])
async def test_40940_carries_one_word_twice(
    client: PlainClient, pump: SimPump, swap: int, data: str
) -> None:
    pump.registers32[40940] = -226  # degree minutes -22.6
    pump.registers[WORD_SWAP] = swap
    assert (await client.read(40940, timeout=5)).data == bytes.fromhex(data)


async def test_product_information_and_0x6e(client: PlainClient, pump: SimPump) -> None:
    pump.info_interval_s = 0.3
    info = first_observed(
        client, lambda o: o.telegram is not None and o.telegram.command == PRODUCT_INFO
    )
    address = first_observed(
        client, lambda o: o.telegram is not None and o.telegram.command == MODBUS_ADDRESS
    )
    await client.read(47134, timeout=5)  # makes this client a target
    seen = await asyncio.wait_for(info, 3)
    assert seen.telegram is not None
    assert seen.telegram.payload == b"\x01\x25\xf9" + b"F1245-6 CU"
    assert seen.trailer == bytes((nibe.ACK,))
    after = await asyncio.wait_for(address, 3)
    assert after.telegram is not None
    assert after.telegram.payload == b"\x01"


async def test_a_register_the_pump_lacks_can_stay_silent(
    client: PlainClient, pump: SimPump
) -> None:
    pump.absent.add(43290)
    with pytest.raises(ReadFailed):
        await client.read(43290, timeout=1.5)
    assert 43290 in pump.taken_reads


async def test_a_clamped_write_is_accepted_and_kept_clamped(
    client: PlainClient, pump: SimPump
) -> None:
    pump.clamp[47011] = (-10, 10)
    outcome = await client.write(47011, 0xFFFF_FFF1, timeout=5)  # -15
    assert outcome.result is WriteResult.ACCEPTED
    assert outcome.t_result is not None
    back = await client.read(47011, after=outcome.t_result, timeout=5)
    assert back.data[:2] == (0xFFF6).to_bytes(2, "little")  # -10


async def test_seeded_faults_cost_retries_not_answers(
    client: PlainClient, pump: SimPump, gateway: Gateway
) -> None:
    pump.inject_faults(7, crc=0.05, garbage=0.05, nak=0.1)
    for register, word in ((40004, 0xFFC4), (47134, 30), (47387, 1)):
        r = await client.read(register, timeout=10)
        assert r.data[:2] == word.to_bytes(2, "little")
    await asyncio.sleep(0.5)
    assert {"crc", "garbage"} <= set(pump.faults)
    stats = gateway.bus.stats
    assert stats.crc_errors > 0
    assert stats.invalid_bytes > 0


async def test_a_nak_means_the_request_wasnt_taken(client: PlainClient, pump: SimPump) -> None:
    pump.inject_faults(1, nak=1.0)
    with pytest.raises(ReadFailed):
        await client.read(47134, timeout=1.0)
    assert pump.faults["nak"] > 0
    assert pump.taken_reads == []
    pump.inject_faults(1)
    assert (await client.read(47134, timeout=5)).data[:2] == (30).to_bytes(2, "little")


async def test_a_pause_reads_as_silence(client: PlainClient, pump: SimPump) -> None:
    await client.read(47134, timeout=5)
    assert client.health().up
    pump.pause(1.6)
    await asyncio.sleep(1.4)
    assert not client.health().up
    await client.read(47134, timeout=5)
    assert client.health().up


async def test_another_client_beside_a_plain_one(
    client: PlainClient, pump: SimPump, other: OtherClient
) -> None:
    await client.read(40004, timeout=5)  # makes this client a target
    theirs = first_observed(client, lambda o: o.reply == nibe.write_request(47387, 0))
    other.write(47387, 0)
    ours = await client.write(47011, 0xFFFE, timeout=5)
    assert ours.result is WriteResult.ACCEPTED
    await asyncio.wait_for(theirs, 3)
    assert {(47387, 0), (47011, 0xFFFE)} <= set(pump.taken_writes)


async def test_another_client_beside_the_protocol(
    gateway: Gateway, pump: SimPump, other: OtherClient
) -> None:
    settings = TgwSettings(
        hello_wait_s=0.3,
        fate_wait_s=0.5,
        answer_timeout_ms=500,
        retry_s=0.1,
        rehello_s=0.2,
        tick_s=0.05,
        health_interval_s=1,
        lease_s=10,
    )
    c = TgwClient("127.0.0.1", gateway.ports["control"], settings=settings)
    await c.start()
    try:
        for _ in range(3):
            other.read(47134)
            other.write(47011, 1)
        r = await c.read(47134, timeout=5)
        assert r.data[:2] == (30).to_bytes(2, "little")
        outcome = await c.write(47387, 0, timeout=5)
        assert outcome.result is WriteResult.ACCEPTED
    finally:
        await c.close()
