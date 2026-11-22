"""What the gateway client makes of each fate a write can meet, and of what else a gateway may
send: against a gateway whose every answer the test sets, so the failures the real one
rarely shows can be had on cue."""

import asyncio
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field, replace

import pytest
from simpump import read_answer, telegram, write_answer
from thermaestro_gateway import nibe
from thermaestro_gateway import protocol as p
from thermaestro_gateway.protocol import Stage

from thermaestro import clock
from thermaestro.nibe.transport.base import Observed, ReadFailed, WriteResult
from thermaestro.nibe.transport.tgw import HandshakeFailed, HandshakeIssue, TgwClient, TgwSettings

SETTINGS = TgwSettings(
    hello_wait_s=0.3,
    hello_tries=2,
    fate_wait_s=0.3,
    answer_timeout_ms=300,
    retry_s=0.05,
    rehello_s=0.1,
    tick_s=0.05,
    lease_s=10,
)
REGISTER = 47011
Sent = p.Message | tuple[float, p.Message]
"""A message the gateway sends, at once or this many seconds later."""


def now_us() -> int:
    return time.monotonic_ns() // 1000


@dataclass(eq=False)
class Scripted(asyncio.DatagramProtocol):
    """A gateway that welcomes every HELLO, signing the session with `psk` where it has one,
    and answers each REQUEST and CANCEL as told."""

    on_request: Callable[[p.Request, int], list[Sent]] = lambda r, n: []
    """What it sends for the n-th request (from 0)."""
    on_cancel: Callable[[p.Cancel], list[Sent]] = lambda c: []
    on_hello: Callable[[p.Hello], bytes] | None = None
    """Its own answer to HELLO, sent as it is, in place of a WELCOME."""
    boot: int = 0x1234
    welcoming: bool = True
    granted: tuple[p.Option, ...] = ()
    """More options in WELCOME: a subscription, a dead-man."""
    psk: bytes | None = None
    key: bytes | None = None
    seq: int = 1
    requests: list[p.Request] = field(default_factory=list)
    cancels: list[int] = field(default_factory=list)
    keepalives: int = 0
    arms: int = 0
    client: tuple[str, int] | None = None
    transport: asyncio.DatagramTransport | None = None

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        assert isinstance(transport, asyncio.DatagramTransport)
        self.transport = transport

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        self.client = addr
        msg = p.decode(data).message
        match msg:
            case p.Hello() if self.on_hello is not None:
                self.raw(self.on_hello(msg))
            case p.Hello() if self.welcoming:
                self.welcome(msg)
            case p.Request():
                self.requests.append(msg)
                for sent in self.on_request(msg, len(self.requests) - 1):
                    self.send(sent)
            case p.Cancel():
                self.cancels.append(msg.id)
                for sent in self.on_cancel(msg):
                    self.send(sent)
            case p.Keepalive():
                self.keepalives += 1
            case p.Arm():
                self.arms += 1  # never answered

    def welcome(self, hello: p.Hello) -> None:
        options = [p.Option.u32(p.Tag.BOOT_ID, self.boot), *self.granted]
        if self.psk is not None:
            client_nonce = p.find(hello.options, p.Tag.CLIENT_NONCE)
            assert client_nonce is not None
            nonce = bytes(range(16))
            self.key = p.session_key(self.psk, client_nonce.value, nonce, self.boot)
            self.seq = 0
            options.append(p.Option(p.Tag.GATEWAY_NONCE, nonce))
        self.send(p.Welcome(id=hello.id, gw_time_us=now_us(), options=tuple(options)))

    def signed(self, msg: p.Message) -> bytes:
        if self.key is None:
            return p.encode(msg)
        self.seq += 1
        return p.encode(msg, key=self.key, seq=self.seq)

    def send(self, sent: Sent) -> None:
        delay, msg = sent if isinstance(sent, tuple) else (0.0, sent)
        assert self.transport is not None
        assert self.client is not None
        data, to = self.signed(msg), self.client
        if delay:
            asyncio.get_running_loop().call_later(delay, self.transport.sendto, data, to)
        else:
            self.transport.sendto(data, to)

    def raw(self, data: bytes) -> None:
        assert self.transport is not None
        assert self.client is not None
        self.transport.sendto(data, self.client)


def fate(request: p.Request | p.Cancel, stage: Stage, detail: int = 0) -> p.Fate:
    t = now_us()
    return p.Fate(id=request.id, stage=stage, detail=detail, stage_time_us=t, gw_time_us=t)


def answer(request: p.Request, status: p.AnswerStatus, frame: bytes) -> p.Answer:
    return p.Answer(id=request.id, status=status, frame=frame, gw_time_us=now_us())


def taken(request: p.Request) -> list[Sent]:
    return [fate(request, Stage.QUEUED), fate(request, Stage.SENT), fate(request, Stage.PUMP_ACK)]


def gone(code: p.ErrorCode = p.ErrorCode.NO_SESSION) -> p.Error:
    """Unsigned and for no request: the gateway doesn't know this client."""
    return p.Error(code=code, gw_time_us=now_us())


@pytest.fixture
async def gateway() -> AsyncIterator[Scripted]:
    loop = asyncio.get_running_loop()
    transport, protocol = await loop.create_datagram_endpoint(Scripted, local_addr=("127.0.0.1", 0))
    try:
        yield protocol
    finally:
        transport.close()


def connect(
    gateway: Scripted, psk: bytes | None = None, settings: TgwSettings = SETTINGS
) -> TgwClient:
    """A client of the scripted gateway, not started yet."""
    assert gateway.transport is not None
    port = gateway.transport.get_extra_info("sockname")[1]
    client_id = bytes(range(0xC0, 0xD0)) if psk is not None else None
    return TgwClient("127.0.0.1", port, psk=psk, client_id=client_id, settings=settings)


@pytest.fixture
async def client(gateway: Scripted) -> AsyncIterator[TgwClient]:
    c = connect(gateway)
    await c.start()
    try:
        yield c
    finally:
        await c.close()


# --- a write's fate -----------------------------------------------------------------------


async def test_a_write_the_pump_naks(client: TgwClient, gateway: Scripted) -> None:
    gateway.on_request = lambda r, n: [
        fate(r, Stage.QUEUED),
        fate(r, Stage.SENT),
        fate(r, Stage.PUMP_NAK),
    ]
    outcome = await client.write(REGISTER, 2, timeout=5)
    assert (outcome.result, outcome.why) == (WriteResult.NOT_TAKEN, "the pump NAKed the frame")
    assert [s.stage for s in outcome.stages] == [Stage.QUEUED, Stage.SENT, Stage.PUMP_NAK]


@pytest.mark.parametrize(
    ("stages", "error", "result", "why"),
    [
        (
            [Stage.QUEUED, Stage.SENT],
            gone(),
            WriteResult.UNKNOWN,
            "taken by the pump; then the session ended",
        ),
        (
            [Stage.QUEUED],
            gone(),
            WriteResult.NOT_TAKEN,
            "the gateway restarted with it queued",
        ),
        (
            [Stage.QUEUED],
            p.Error(code=p.ErrorCode.AUTH_FAILED),
            WriteResult.UNKNOWN,
            "the session ended with it queued",
        ),
    ],
    ids=["after-the-pump-took-it", "queued-gateway-restarted", "queued-session-ended"],
)
async def test_a_write_whose_session_ends(
    client: TgwClient,
    gateway: Scripted,
    stages: list[Stage],
    error: p.Error,
    result: WriteResult,
    why: str,
) -> None:
    gateway.on_request = lambda r, n: [*(fate(r, s) for s in stages), (0.05, error)]
    outcome = await client.write(REGISTER, 2, timeout=5)
    assert (outcome.result, outcome.why) == (result, why)
    assert outcome.t_result is None


async def test_a_write_dropped_for_a_full_queue_is_sent_again(
    client: TgwClient, gateway: Scripted
) -> None:
    def script(r: p.Request, n: int) -> list[Sent]:
        if n == 0:
            return [fate(r, Stage.DROPPED, p.DropReason.QUEUE_FULL)]
        return [*taken(r), answer(r, p.AnswerStatus.OK, write_answer(1))]

    gateway.on_request = script
    outcome = await client.write(REGISTER, 2, timeout=5)
    assert outcome.result is WriteResult.ACCEPTED
    assert len(gateway.requests) == 2
    assert [s.stage for s in outcome.stages] == [  # the first attempt's too
        Stage.DROPPED,
        Stage.QUEUED,
        Stage.SENT,
        Stage.PUMP_ACK,
    ]


async def test_a_write_dropped_for_a_full_queue_until_its_deadline(
    client: TgwClient, gateway: Scripted
) -> None:
    gateway.on_request = lambda r, n: [fate(r, Stage.DROPPED, p.DropReason.QUEUE_FULL)]
    outcome = await client.write(REGISTER, 2, timeout=0.5)
    assert outcome.result is WriteResult.NOT_TAKEN
    # The last try's drop comes before the deadline, or after it, to the cancel's wait.
    assert outcome.why in (
        "dropped by the gateway: QUEUE_FULL",
        "not sent in time: QUEUE_FULL",
    )
    assert len(gateway.requests) > 1


@pytest.mark.parametrize(
    ("reason", "why"),
    [
        (p.DropReason.EXPIRED, "dropped by the gateway: EXPIRED"),
        (p.DropReason.TOKEN_MISMATCH, "dropped by the gateway: TOKEN_MISMATCH"),
        (99, "dropped by the gateway: reason 99"),
    ],
)
async def test_a_write_dropped_for_good(
    client: TgwClient, gateway: Scripted, reason: int, why: str
) -> None:
    gateway.on_request = lambda r, n: [fate(r, Stage.QUEUED), fate(r, Stage.DROPPED, reason)]
    outcome = await client.write(REGISTER, 2, timeout=5)
    assert (outcome.result, outcome.why) == (WriteResult.NOT_TAKEN, why)
    assert len(gateway.requests) == 1


@pytest.mark.parametrize(
    ("code", "why"),
    [
        (p.ErrorCode.SOURCE_REFUSED, "refused by the gateway: source_refused"),
        (p.ErrorCode.MALFORMED, "refused by the gateway: malformed"),
        (77, "refused by the gateway: error 77"),
    ],
)
async def test_a_write_the_gateway_refuses(
    client: TgwClient, gateway: Scripted, code: int, why: str
) -> None:
    gateway.on_request = lambda r, n: [p.Error(id=r.id, code=code, gw_time_us=now_us())]
    outcome = await client.write(REGISTER, 2, timeout=5)
    assert (outcome.result, outcome.why) == (WriteResult.NOT_TAKEN, why)


@pytest.mark.parametrize(
    ("status", "frame", "result", "why"),
    [
        (p.AnswerStatus.OK, write_answer(1), WriteResult.ACCEPTED, "0x6C = 1"),
        (p.AnswerStatus.OK, write_answer(0), WriteResult.REFUSED, "0x6C = 0"),
        (p.AnswerStatus.OK, write_answer(7), WriteResult.UNKNOWN, "0x6C = 7"),
        (
            p.AnswerStatus.TIMEOUT,
            b"",
            WriteResult.UNKNOWN,
            "taken by the pump, but no 0x6C came",
        ),
        (
            p.AnswerStatus.AMBIGUOUS,
            write_answer(1),
            WriteResult.UNKNOWN,
            "a 0x6C came with another write in flight (ambiguous)",
        ),
        (p.AnswerStatus.OK, b"\x5c\x00", WriteResult.UNKNOWN, "the answer didn't parse"),
        (
            p.AnswerStatus.OK,
            read_answer(REGISTER, {REGISTER: 2}),
            WriteResult.UNKNOWN,
            "the answer wasn't a 0x6C",
        ),
        (
            p.AnswerStatus.OK,
            telegram(nibe.MODBUS40, nibe.WRITE_ANSWER, b""),
            WriteResult.UNKNOWN,
            "the answer wasn't a 0x6C",
        ),
    ],
    ids=["1", "0", "7", "timeout", "ambiguous", "unparsed", "a-0x6a", "empty"],
)
async def test_what_the_pumps_answer_says(
    client: TgwClient,
    gateway: Scripted,
    status: p.AnswerStatus,
    frame: bytes,
    result: WriteResult,
    why: str,
) -> None:
    gateway.on_request = lambda r, n: [*taken(r), answer(r, status, frame)]
    outcome = await client.write(REGISTER, 2, timeout=5)
    assert (outcome.result, outcome.why) == (result, why)


# --- a write the deadline or the gateway's silence cuts short -----------------------------


async def test_a_write_the_gateway_never_reports_on_is_canceled(
    client: TgwClient, gateway: Scripted
) -> None:
    outcome = await client.write(REGISTER, 2, timeout=5)
    assert (outcome.result, outcome.why) == (
        WriteResult.UNKNOWN,
        "the gateway never reported on it",
    )
    assert gateway.cancels == [gateway.requests[0].id]
    assert client.counters["requests_unanswered"] == 1


async def test_a_write_still_queued_at_its_deadline(client: TgwClient, gateway: Scripted) -> None:
    gateway.on_request = lambda r, n: [fate(r, Stage.QUEUED)]
    outcome = await client.write(REGISTER, 2, timeout=0.5)
    assert (outcome.result, outcome.why) == (
        WriteResult.UNKNOWN,
        "still queued at the deadline; canceling wasn't confirmed",
    )
    assert gateway.cancels == [gateway.requests[0].id]


async def test_a_write_canceled_while_queued_was_never_sent(
    client: TgwClient, gateway: Scripted
) -> None:
    gateway.on_request = lambda r, n: [fate(r, Stage.QUEUED)]
    gateway.on_cancel = lambda c: [fate(c, Stage.DROPPED, p.DropReason.CANCELED)]
    outcome = await client.write(REGISTER, 2, timeout=0.5)
    assert (outcome.result, outcome.why) == (
        WriteResult.NOT_TAKEN,
        "not sent in time: CANCELED",
    )


async def test_a_write_the_pump_took_is_answered_after_its_deadline(
    client: TgwClient, gateway: Scripted
) -> None:
    gateway.on_request = lambda r, n: [
        *taken(r),
        (0.7, answer(r, p.AnswerStatus.OK, write_answer(1))),
    ]
    outcome = await client.write(REGISTER, 2, timeout=0.4)
    assert (outcome.result, outcome.why) == (WriteResult.ACCEPTED, "0x6C = 1")
    assert gateway.cancels == []  # it had reached the pump: nothing to cancel


async def test_a_write_the_pump_took_and_no_answer_came(
    client: TgwClient, gateway: Scripted
) -> None:
    gateway.on_request = lambda r, n: taken(r)
    outcome = await client.write(REGISTER, 2, timeout=0.4)
    assert (outcome.result, outcome.why) == (
        WriteResult.UNKNOWN,
        "taken by the pump; no result came",
    )


async def test_a_write_with_no_session_to_send_it_in(client: TgwClient, gateway: Scripted) -> None:
    gateway.welcoming = False
    gateway.send(gone())
    await until(lambda: not client.health().up)
    outcome = await client.write(REGISTER, 2, timeout=0.5)
    assert (outcome.result, outcome.why) == (WriteResult.NOT_TAKEN, "no session with the gateway")
    assert gateway.requests == []
    await until(lambda: client.counters["hello_failures"])
    gateway.welcoming = True  # and the session comes back
    await until(lambda: client.health().up)


# --- what else the gateway may send -------------------------------------------------------


async def until(condition: Callable[[], object], seconds: float = 3.0) -> None:
    async with asyncio.timeout(seconds):
        while not condition():
            await asyncio.sleep(0.01)


async def test_what_cant_be_read_or_isnt_for_this_session_is_counted(
    client: TgwClient, gateway: Scripted
) -> None:
    gateway.raw(b"not the protocol")
    signed = p.encode(p.Keepalive(gw_time_us=now_us()), key=bytes(32), seq=5)
    gateway.raw(signed)
    gateway.send(p.Welcome(id=999, gw_time_us=now_us()))
    gateway.send(p.Error(id=4242, code=p.ErrorCode.MALFORMED, gw_time_us=now_us()))
    await until(lambda: client.counters["errors_malformed"])
    assert client.counters["unreadable"] == 1
    assert client.counters["unexpectedly_signed"] == 1
    assert client.counters["unexpected_messages"] == 1
    assert client.health().up


async def test_a_fate_of_a_stage_not_known_is_counted(client: TgwClient, gateway: Scripted) -> None:
    gateway.on_request = lambda r, n: [
        fate(r, Stage.QUEUED),
        p.Fate(id=r.id, stage=42, stage_time_us=now_us(), gw_time_us=now_us()),
        fate(r, Stage.SENT),
        fate(r, Stage.PUMP_ACK),
        answer(r, p.AnswerStatus.OK, write_answer(1)),
    ]
    outcome = await client.write(REGISTER, 2, timeout=5)
    assert outcome.result is WriteResult.ACCEPTED
    assert client.counters["unknown_stages"] == 1


async def test_health_is_kept_and_a_new_boot_in_it_ends_the_session(
    client: TgwClient, gateway: Scripted
) -> None:
    sessions: list[None] = []
    client.on_session(lambda: sessions.append(None))
    options = (
        p.Option.u8(p.Tag.BUS_STATE, 1),
        p.Option.pack(p.Tag.DROPS, p.DropReason.QUEUE_FULL, 3),
        p.Option.pack(p.Tag.QUEUE_DEPTH, nibe.MODBUS40, nibe.WRITE_TOKEN, 1),
        p.Option(0x7F00, b"\x01"),  # a newer gateway's
        p.Option.pack(p.Tag.PLAIN_PORTS, 9999, 10000),  # not one number
    )
    gateway.send(p.Health(id=1, gw_time_us=now_us(), options=options))
    await until(lambda: client.gateway_values)
    assert client.counters["unreadable_health_values"] == 1
    assert client.gateway_values == {
        "BUS_STATE": 1,
        "DROPS_QUEUE_FULL": 3,
        f"QUEUE_DEPTH_{nibe.MODBUS40:#06x}_{nibe.WRITE_TOKEN:#04x}": 1,
    }
    gateway.send(p.Health(id=5, gw_time_us=now_us()))
    await until(lambda: client.counters["events_missed"])
    assert client.counters["events_missed"] == 3
    gateway.boot = 0x5678
    restarted = (p.Option.u32(p.Tag.BOOT_ID, 0x5678),)
    gateway.send(p.Health(id=6, gw_time_us=now_us(), options=restarted))
    await until(lambda: sessions and client.boot_id == 0x5678)
    assert client.health().up


async def test_a_cancel_the_gateway_doesnt_know_is_marked(
    client: TgwClient, gateway: Scripted
) -> None:
    gateway.on_request = lambda r, n: [fate(r, Stage.QUEUED)]
    gateway.on_cancel = lambda c: [
        p.Error(id=c.id, code=p.ErrorCode.UNKNOWN_REQUEST, gw_time_us=now_us())
    ]
    outcome = await client.write(REGISTER, 2, timeout=0.5)
    assert outcome.result is WriteResult.UNKNOWN
    assert gateway.cancels


async def test_a_frame_that_doesnt_parse_is_still_observed(
    client: TgwClient, gateway: Scripted
) -> None:
    seen: list[Observed] = []
    client.observe(seen.append)
    gateway.send(
        p.Frame(kind=3, origin=99, t_complete_us=now_us(), data=b"\x01\x02", gw_time_us=now_us())
    )
    await until(lambda: seen)
    assert (seen[0].data, seen[0].telegram, seen[0].origin) == (b"\x01\x02", None, None)


# --- the session --------------------------------------------------------------------------

PSK = bytes(range(32))


def welcome(hello: p.Hello, *options: p.Option) -> bytes:
    return p.encode(p.Welcome(id=hello.id, gw_time_us=now_us(), options=options))


def boot(value: int = 0x1234) -> p.Option:
    return p.Option.u32(p.Tag.BOOT_ID, value)


def refusal(code: p.ErrorCode, text: str = "") -> Callable[[p.Hello], bytes]:
    detail = (p.Option.text(p.Tag.ERR_DETAIL, text),) if text else ()
    return lambda h: p.encode(p.Error(id=h.id, code=code, options=detail))


@pytest.mark.parametrize(
    ("psk", "on_hello", "issue", "why"),
    [
        (None, lambda h: bytes(24), HandshakeIssue.VERSION, "bad_magic"),
        (None, lambda h: b"short", HandshakeIssue.REFUSED, "an unreadable answer: malformed"),
        (None, lambda h: welcome(h), HandshakeIssue.REFUSED, "WELCOME without BOOT_ID"),
        (None, refusal(p.ErrorCode.BAD_VERSION), HandshakeIssue.VERSION, ""),
        (
            None,
            refusal(p.ErrorCode.TOO_MANY_CLIENTS, "four already"),
            HandshakeIssue.REFUSED,
            "too_many_clients four already",
        ),
        (PSK, lambda h: welcome(h, boot()), HandshakeIssue.AUTH_FAILED, "WELCOME without a nonce"),
        (
            PSK,
            lambda h: welcome(h, boot(), p.Option(p.Tag.GATEWAY_NONCE, bytes(16))),
            HandshakeIssue.AUTH_FAILED,
            "WELCOME isn't signed with the session key",
        ),
    ],
    ids=["not-the-protocol", "cut-short", "no-boot", "version", "refused", "no-nonce", "unsigned"],
)
async def test_a_hello_answered_amiss(
    gateway: Scripted,
    psk: bytes | None,
    on_hello: Callable[[p.Hello], bytes],
    issue: HandshakeIssue,
    why: str,
) -> None:
    gateway.on_hello = on_hello
    c = connect(gateway, psk)
    with pytest.raises(HandshakeFailed) as failed:
        await c.start()
    await c.close()
    assert failed.value.issue is issue
    assert failed.value.detail.startswith(why)


async def test_a_lease_with_no_health_ends_the_session(gateway: Scripted) -> None:
    gateway.granted = (p.Option.u32(p.Tag.SUBSCRIBE, p.Subscription.HEALTH),)
    c = connect(gateway, settings=replace(SETTINGS, lease_s=1))
    await c.start()
    try:
        sessions: list[None] = []
        c.on_session(lambda: sessions.append(None))
        await until(lambda: gateway.keepalives)  # a third of the lease without a word
        await until(lambda: sessions)  # the lease without HEALTH: lost, and said HELLO again
        assert c.health().up
    finally:
        await c.close()


@pytest.fixture
async def keyed(gateway: Scripted) -> AsyncIterator[TgwClient]:
    gateway.psk = PSK
    gateway.granted = (p.Option.pack(p.Tag.DEADMAN, 900, 16, 0, 1),)
    c = connect(gateway, PSK, replace(SETTINGS, deadman_wait_s=0.1, reports_wait_s=0.2))
    await c.start()
    try:
        yield c
    finally:
        await c.close()


async def test_a_signed_session_takes_only_what_is_signed_anew(
    keyed: TgwClient, gateway: Scripted
) -> None:
    gateway.on_request = lambda r, n: [*taken(r), answer(r, p.AnswerStatus.OK, write_answer(1))]
    assert (await keyed.write(REGISTER, 2, timeout=5)).result is WriteResult.ACCEPTED
    assert gateway.key is not None
    keepalive = p.Keepalive(gw_time_us=now_us())
    gateway.raw(p.encode(keepalive, key=bytes(32), seq=gateway.seq + 1))  # another key
    gateway.raw(p.encode(keepalive))  # not signed
    gateway.raw(p.encode(keepalive, key=gateway.key, seq=1))  # sent before
    await until(lambda: keyed.counters["replays"])
    assert keyed.counters["auth_failures"] == 2
    assert keyed.health().up


async def test_the_dead_man_unanswered_and_its_reports_awaited(
    keyed: TgwClient, gateway: Scripted
) -> None:
    assert keyed.deadman is not None
    assert keyed.deadman.fired == 1
    assert not await keyed.reports_in()  # announced in WELCOME, never sent
    back = nibe.write_request(REGISTER, 1)
    assert await keyed.arm(0, 2, REGISTER, 2, back) is None
    assert gateway.arms == keyed.settings.deadman_tries
    report = p.Fired(entry=3, outcome=p.FiredOutcome.PUT_BACK, register=REGISTER, fired_us=0)
    gateway.send(report)
    await until(lambda: keyed.fired)
    heard: list[p.Fired] = []
    keyed.on_fired(heard.append)  # told at once of what came before it listened
    assert [r.entry for r in heard] == [3]
    assert await keyed.reports_in()


def test_no_time_from_the_gateway_before_it_has_said_anything() -> None:
    with pytest.raises(RuntimeError, match="no message from the gateway yet"):
        TgwClient("127.0.0.1").local_time(0)


# --- reads --------------------------------------------------------------------------------


def reading(request: p.Request, register: int = REGISTER, value: int = 2) -> list[Sent]:
    return [
        *taken(request),
        answer(request, p.AnswerStatus.OK, read_answer(register, {register: value})),
    ]


async def test_a_read_dropped_for_a_full_queue_is_asked_again(
    client: TgwClient, gateway: Scripted
) -> None:
    def script(r: p.Request, n: int) -> list[Sent]:
        if n == 0:
            return [fate(r, Stage.DROPPED, p.DropReason.QUEUE_FULL)]
        return reading(r)

    gateway.on_request = script
    r = await client.read(REGISTER, timeout=5)
    assert r.data[:2] == (2).to_bytes(2, "little")
    assert r.stages[0].stage is Stage.DROPPED


@pytest.mark.parametrize(
    ("script", "why"),
    [
        (
            lambda r, n: [fate(r, Stage.DROPPED, p.DropReason.EXPIRED)],
            "dropped by the gateway: EXPIRED",
        ),
        (
            lambda r, n: [p.Error(id=r.id, code=p.ErrorCode.SOURCE_REFUSED, gw_time_us=now_us())],
            "refused by the gateway: source_refused",
        ),
        (lambda r, n: reading(r, REGISTER + 1), "the answer wasn't a reading of it"),
        (
            lambda r, n: [*taken(r), answer(r, p.AnswerStatus.TIMEOUT, b"")],
            "the answer wasn't a reading of it",
        ),
        (
            lambda r, n: [*taken(r), answer(r, p.AnswerStatus.OK, b"\x5c\x00")],
            "the answer wasn't a reading of it",
        ),
        (
            lambda r, n: [*taken(r), answer(r, p.AnswerStatus.OK, write_answer(1))],
            "the answer wasn't a reading of it",
        ),
    ],
    ids=["dropped", "refused", "another-register", "timeout", "unparsed", "a-0x6c"],
)
async def test_a_read_that_fails(
    client: TgwClient,
    gateway: Scripted,
    script: Callable[[p.Request, int], list[Sent]],
    why: str,
) -> None:
    gateway.on_request = script
    with pytest.raises(ReadFailed) as failed:
        await client.read(REGISTER, timeout=0.5)
    assert failed.value.why == why


async def test_a_read_taken_before_it_was_asked_for_isnt_given(
    client: TgwClient, gateway: Scripted
) -> None:
    gateway.on_request = lambda r, n: reading(r)
    with pytest.raises(ReadFailed) as failed:
        await client.read(REGISTER, after=clock.monotonic() + 60, timeout=0.5)
    assert failed.value.why.startswith("no answer within")
    assert len(gateway.requests) > 1


async def test_a_read_with_no_session_to_send_it_in(client: TgwClient, gateway: Scripted) -> None:
    gateway.welcoming = False
    gateway.send(gone())
    await until(lambda: not client.health().up)
    with pytest.raises(ReadFailed) as failed:
        await client.read(REGISTER, timeout=0.3)
    assert failed.value.why == "no session with the gateway"


def test_a_key_and_a_client_id_must_be_their_size() -> None:
    with pytest.raises(ValueError, match="32 bytes"):
        TgwClient("127.0.0.1", psk=bytes(31))
    with pytest.raises(ValueError, match="16 bytes"):
        TgwClient("127.0.0.1", psk=bytes(32), client_id=bytes(15))
