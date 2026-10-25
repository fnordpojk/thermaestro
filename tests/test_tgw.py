import asyncio
import socket
from collections.abc import AsyncIterator, Callable
from dataclasses import replace

import pytest
from simpump import TEST_PSK as PSK
from simpump import SimPump
from thermaestro_gateway import nibe
from thermaestro_gateway import protocol as p
from thermaestro_gateway.deadman import Outcome
from thermaestro_gateway.protocol import Origin, Stage
from thermaestro_gateway.server import Config, Gateway

from thermaestro.nibe.transport.base import FateKind, Observed, ReadFailed, WriteResult
from thermaestro.nibe.transport.tgw import (
    DeadmanGrant,
    GatewayClock,
    HandshakeFailed,
    HandshakeIssue,
    TgwClient,
    TgwSettings,
)

FAST = TgwSettings(
    hello_wait_s=0.3,
    fate_wait_s=0.5,
    answer_timeout_ms=500,
    retry_s=0.1,
    rehello_s=0.2,
    tick_s=0.05,
    health_interval_s=1,
    lease_s=10,
)


def client_for(gateway: Gateway, psk: bytes | None = None) -> TgwClient:
    return TgwClient("127.0.0.1", gateway.ports["control"], psk=psk, settings=FAST)


@pytest.fixture
async def client(gateway: Gateway) -> AsyncIterator[TgwClient]:
    c = client_for(gateway)
    await c.start()
    yield c
    await c.close()


def word(value: int) -> bytes:
    return value.to_bytes(2, "little")


async def test_the_handshake(client: TgwClient) -> None:
    promises = client.promises
    assert promises.fate is FateKind.EXACT
    assert promises.sees_other_writers
    assert promises.gateway_timestamps
    assert client.health().up
    assert client.boot_id is not None


async def test_a_read(client: TgwClient) -> None:
    r = await client.read(47134, timeout=5)
    assert (r.register, r.data) == (47134, word(30) + word(30))
    assert [s.stage for s in r.stages] == [Stage.QUEUED, Stage.SENT, Stage.PUMP_ACK]
    assert r.t_taken is not None
    assert r.t_taken < r.t_answered


async def test_a_write_is_accepted_and_reads_back(client: TgwClient, pump: SimPump) -> None:
    outcome = await client.write(47011, 0xFFFE, timeout=5)
    assert outcome.result is WriteResult.ACCEPTED
    assert outcome.t_result is not None
    back = await client.read(47011, after=outcome.t_result, timeout=5)
    assert back.data[:2] == word(0xFFFE)
    assert pump.taken_writes == [(47011, 0xFFFE)]


async def test_a_refused_write(client: TgwClient, pump: SimPump) -> None:
    pump.refuse.add(47011)
    assert (await client.write(47011, 1, timeout=5)).result is WriteResult.REFUSED


async def test_accepted_isnt_kept(client: TgwClient, pump: SimPump) -> None:
    pump.not_kept.add(47387)
    outcome = await client.write(47387, 0, timeout=5)
    assert outcome.result is WriteResult.ACCEPTED
    assert outcome.t_result is not None
    back = await client.read(47387, after=outcome.t_result, timeout=5)
    assert back.data[:2] == word(1)


async def test_a_read_back_isnt_given_a_read_taken_before_the_write(
    client: TgwClient, pump: SimPump, gateway: Gateway
) -> None:
    # A plain client's read is taken just before the write and answered, with the old
    # value, after the write's 0x6C. The gateway pairs it with the plain read.
    pump.answer_delay_s = 0.4
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as other:
        other.sendto(nibe.read_request(47011), ("127.0.0.1", gateway.ports["read"]))
        await asyncio.sleep(0.05)
        outcome = await client.write(47011, 5, timeout=5)
        assert outcome.t_result is not None
        back = await client.read(47011, after=outcome.t_result, timeout=5)
    assert pump.taken_reads[0] == 47011
    assert back.data[:2] == word(5)


async def test_a_read_that_is_never_answered_fails(client: TgwClient, pump: SimPump) -> None:
    pump.silent_reads = 1000
    with pytest.raises(ReadFailed):
        await client.read(47134, timeout=1.5)


async def test_frames_say_whose_request_was_sent(client: TgwClient) -> None:
    seen: list[Observed] = []
    client.observe(seen.append)
    await client.read(47134, timeout=5)
    ours = [o for o in seen if o.reply == nibe.read_request(47134)]
    assert ours
    assert ours[0].origin is Origin.THIS_CLIENT


async def test_with_a_key(gateway_with_psk: Gateway) -> None:
    c = client_for(gateway_with_psk, PSK)
    await c.start()
    try:
        r = await c.read(47134, timeout=5)
        assert r.data[:2] == word(30)
    finally:
        await c.close()


@pytest.fixture
async def keyed(gateway_with_psk: Gateway) -> AsyncIterator[TgwClient]:
    c = client_for(gateway_with_psk, PSK)
    await c.start()
    yield c
    await c.close()


async def until(condition: Callable[[], object], seconds: float = 3.0) -> None:
    async with asyncio.timeout(seconds):
        while not condition():
            await asyncio.sleep(0.01)


async def test_with_a_key_the_dead_man_is_asked_for_and_granted(keyed: TgwClient) -> None:
    assert keyed.deadman == DeadmanGrant(seconds=900, entries=16, armed=0, fired=0)
    assert keyed.promises.deadman


async def test_without_a_key_there_is_no_dead_man(client: TgwClient) -> None:
    assert client.deadman is None
    assert not client.promises.deadman
    assert await client.arm(0, 2, 47011, 0xFFFE, nibe.write_request(47011, 0xFFFC)) is None


async def test_arming_and_disarming(keyed: TgwClient, gateway_with_psk: Gateway) -> None:
    held = gateway_with_psk.control.deadman
    assert held is not None
    back = nibe.write_request(47011, 0xFFFC)
    armed = await keyed.arm(0, 2, 47011, 0xFFFE, back)
    assert armed is not None
    assert armed.result == p.ArmResult.ARMED
    assert held.armed[0].expected == 0xFFFE
    elsewhere = await keyed.arm(1, 2, 47012, 0xFFFE, back)
    assert elsewhere is not None
    assert (elsewhere.result, elsewhere.detail) == (
        p.ArmResult.REFUSED,
        p.ArmRefusal.REGISTER_MISMATCH,
    )
    disarmed = await keyed.disarm(0)
    assert disarmed is not None
    assert disarmed.result == p.ArmResult.DISARMED
    assert held.armed == {}


async def test_a_fired_report_waits_until_it_is_acknowledged(gateway_with_psk: Gateway) -> None:
    """The gateway reports what it put back right after WELCOME; a listener that comes
    later still hears it, and once acknowledged, the gateway forgets it."""
    held = gateway_with_psk.control.deadman
    assert held is not None
    held.fired[3] = Outcome(3, p.FiredOutcome.PUT_BACK, 47011, 0xFFFC)
    c = client_for(gateway_with_psk, PSK)
    await c.start()
    try:
        assert c.deadman is not None
        assert c.deadman.fired == 1
        heard: list[p.Fired] = []
        c.on_fired(heard.append)
        await until(lambda: heard)
        assert (heard[0].entry, heard[0].outcome, heard[0].register) == (
            3,
            p.FiredOutcome.PUT_BACK,
            47011,
        )
        acknowledged = await c.ack_fired(3)
        assert acknowledged is not None
        assert acknowledged.result == p.ArmResult.DISARMED
        assert held.fired == {}
        assert c.fired == {}
    finally:
        await c.close()


async def test_a_new_session_is_told(keyed: TgwClient) -> None:
    sessions: list[None] = []
    keyed.on_session(lambda: sessions.append(None))
    keyed._lose_session("a test", gone=False)
    await until(lambda: sessions)
    assert keyed.deadman is not None


@pytest.mark.parametrize(
    ("psk", "issue"),
    [(None, HandshakeIssue.AUTH_REQUIRED), (bytes(32), HandshakeIssue.AUTH_FAILED)],
)
async def test_a_gateway_with_a_key_refuses(
    gateway_with_psk: Gateway, psk: bytes | None, issue: HandshakeIssue
) -> None:
    c = client_for(gateway_with_psk, psk)
    with pytest.raises(HandshakeFailed) as failed:
        await c.start()
    await c.close()
    assert failed.value.issue is issue


async def test_a_key_for_a_gateway_without_one(gateway: Gateway) -> None:
    c = client_for(gateway, PSK)
    with pytest.raises(HandshakeFailed) as failed:
        await c.start()
    await c.close()
    assert failed.value.issue is HandshakeIssue.AUTH_UNSUPPORTED


async def test_silence() -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as quiet:
        quiet.bind(("127.0.0.1", 0))
        c = TgwClient("127.0.0.1", quiet.getsockname()[1], settings=FAST)
        with pytest.raises(HandshakeFailed) as failed:
            await c.start()
        await c.close()
    assert failed.value.issue is HandshakeIssue.SILENT


async def test_a_gateway_restart_starts_a_new_session(
    client: TgwClient, gateway: Gateway, pump: SimPump
) -> None:
    await client.read(47134, timeout=5)
    first_boot = client.boot_id
    ports = dict(gateway.ports)
    await gateway.close()
    restarted = Gateway(
        Config(
            serial_port=pump.end.path,
            listen="127.0.0.1",
            read_port=ports["read"],
            write_port=ports["write"],
            control_port=ports["control"],
        )
    )
    await restarted.start()
    try:
        r = await client.read(47134, timeout=10)
        assert r.data[:2] == word(30)
        assert client.boot_id != first_boot
    finally:
        await restarted.close()


def test_the_clock_keeps_the_smallest_recent_offset() -> None:
    clock = GatewayClock(window_s=60)
    clock.sample(1_000_000, 101.004)  # 4 ms on the way
    clock.sample(2_000_000, 102.001)  # 1 ms
    clock.sample(3_000_000, 103.010)
    assert clock.to_local(5_000_000) == pytest.approx(105.001)
    # A minute later the old samples are forgotten.
    clock.sample(80_000_000, 180.003)
    assert clock.to_local(80_000_000) == pytest.approx(180.003)


async def test_a_closed_client_frees_its_port(gateway: Gateway) -> None:
    """Closed, its socket is: a plugin started again binds the same port at once."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        probe.bind(("0.0.0.0", 0))  # noqa: S104
        port = probe.getsockname()[1]
    c = TgwClient("127.0.0.1", gateway.ports["control"], settings=replace(FAST, local_port=port))
    await c.start()
    await c.close()
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as again:
        again.bind(("0.0.0.0", port))  # noqa: S104
