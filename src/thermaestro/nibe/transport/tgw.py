"""The Thermaestro gateway protocol, client side (docs/gateway-protocol.md §13).

The gateway reports each request's fate (QUEUED, SENT, the pump's ACK or NAK, DROPPED
with a reason) and pairs the pump's answer with it, so nothing here is inferred. Event
times come from the gateway's clock and are mapped onto local time (spec §9).

- One of this client's requests at a time waits in each of the gateway's queues: each
  has one entry for protocol requests, beside the plain clients' (spec §8).
- A request carries a ttl to its caller's deadline, so a write the caller gave up on
  never reaches the pump. At the deadline a queued request is cancelled as well.
- A session ends with a gateway restart (a new boot_id, or `no_session`), or when no
  HEALTH has come for a lease. Then the client says HELLO again until it is back.
- With a pre-shared key, every message is signed (spec §11), HELLO with the key itself. The
  client insists on it: its CLIENT_NONCE is critical, so a gateway without a key refuses
  it instead of opening an unauthenticated session.
"""

import asyncio
import contextlib
import logging
import os
import socket
from collections import Counter, deque
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum

from thermaestro_gateway import nibe
from thermaestro_gateway import protocol as p

from thermaestro import clock, durations
from thermaestro.nibe.transport.base import (
    FateKind,
    LinkHealth,
    Observed,
    Observers,
    Promises,
    ReadFailed,
    Reading,
    StageEvent,
    TransportError,
    WriteOutcome,
    WriteResult,
    wait_any,
)

log = logging.getLogger(__name__)

DELIVERY_S = 0.01
"""What a local network adds to the gateway's own timestamp lag, for `uncertainty_s`."""
GATEWAY_ANSWER_TIMEOUT_S = 5.0
"""The protocol's default answer timeout (spec §7.9), when the client leaves it to the gateway."""

_Outcome = Callable[[WriteResult, str, float | None], WriteOutcome]


class HandshakeIssue(Enum):
    SILENT = "no answer to HELLO"
    VERSION = "the gateway speaks another version of the protocol"
    AUTH_REQUIRED = "the gateway requires a key, and none is configured"
    AUTH_FAILED = "the gateway refused the key"
    AUTH_UNSUPPORTED = "a key is configured, but the gateway has none"
    REFUSED = "the gateway refused the session"


class HandshakeFailed(TransportError):
    def __init__(self, issue: HandshakeIssue, detail: str = "") -> None:
        super().__init__(f"{issue.value} ({detail})" if detail else issue.value)
        self.issue = issue
        self.detail = detail


@dataclass(frozen=True, slots=True)
class TgwSettings:
    local_port: int = 0
    name: str = "thermaestro"
    lease_s: int = 120
    health_interval_s: int = 10
    hello_tries: int = 3
    """Spec §13: HELLO, then twice more, a second apart."""
    hello_wait_s: float = 1.0
    fate_wait_s: float = 2.0
    """No FATE this long after a REQUEST: the request or its fate was lost."""
    answer_timeout_ms: int = 0
    """0 leaves it to the gateway (5 s by default)."""
    retry_s: float = 0.5
    """After QUEUE_FULL or EVICTED, try again this much later."""
    rehello_s: float = 5.0
    tick_s: float = 1.0
    subscribe: p.Subscription = p.Subscription.FRAMES_ALL | p.Subscription.HEALTH


@dataclass(slots=True)
class _Bucket:
    start: float
    offset: float


class GatewayClock:
    """Gateway time to local time (spec §9). Every message from the gateway is a sample: its
    arrival here minus its gw_time is the clocks' offset plus the delay on the way, so the
    smallest recent sample is the best estimate. Samples older than the window are
    forgotten, which follows a drifting crystal."""

    BUCKET_S = 10.0

    def __init__(self, window_s: float = 600.0) -> None:
        self.window_s = window_s
        self._buckets: deque[_Bucket] = deque()

    def reset(self) -> None:
        self._buckets.clear()

    def sample(self, gw_time_us: int, t_local: float) -> None:
        offset = t_local - gw_time_us / 1e6
        last = self._buckets[-1] if self._buckets else None
        if last is not None and t_local - last.start < self.BUCKET_S:
            last.offset = min(last.offset, offset)
        else:
            self._buckets.append(_Bucket(t_local, offset))
        while t_local - self._buckets[0].start > self.window_s:
            self._buckets.popleft()

    def to_local(self, gw_time_us: int) -> float:
        if not self._buckets:
            raise RuntimeError("no message from the gateway yet")
        return gw_time_us / 1e6 + min(b.offset for b in self._buckets)


@dataclass(frozen=True, slots=True)
class _Final:
    answer: p.Answer | None = None
    t: float | None = None
    dropped: int | None = None
    error: int | None = None
    lost: bool = False
    gone: bool = False
    """With `lost`: the gateway restarted, so a queued request is known to be gone."""
    nak: bool = False


@dataclass(eq=False, slots=True)
class _Request:
    id: int
    token: int
    frame: bytes
    final: asyncio.Future[_Final]
    stages: list[StageEvent] = field(default_factory=list)
    fated: asyncio.Event = field(default_factory=asyncio.Event)
    stage: p.Stage | None = None
    t_sent: float | None = None
    cancel_unknown: bool = False


class _SessionDown(Exception):
    pass


class _Socket(asyncio.DatagramProtocol):
    def __init__(self, client: "TgwClient") -> None:
        self.client = client

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        self.client._datagram(data, addr)

    def error_received(self, exc: Exception) -> None:
        self.client.counters["udp_errors"] += 1
        log.debug("gateway control socket: %s", exc)


class TgwClient:
    def __init__(
        self,
        host: str,
        control_port: int = 10090,
        *,
        psk: bytes | None = None,
        settings: TgwSettings | None = None,
    ) -> None:
        if psk is not None and len(psk) != 32:
            raise ValueError("the gateway key is 32 bytes")
        self.host = host
        self.control_port = control_port
        self.psk = psk
        self.settings = settings or TgwSettings()
        self.counters: Counter[str] = Counter()
        self.boot_id: int | None = None
        self.gateway_values: dict[str, int] = {}
        self.welcome: p.Welcome | None = None
        self._observers = Observers()
        self._clock = GatewayClock()
        self._ip = ""
        self._transport: asyncio.DatagramTransport | None = None
        self._ticker: asyncio.Task[None] | None = None
        self._rehello: asyncio.Task[None] | None = None
        self._up = asyncio.Event()
        self._key: bytes | None = None
        self._send_seq = 0
        self._recv_seq = 0
        self._next_id = 0
        self._hello: tuple[int, asyncio.Future[bytes]] | None = None
        self._requests: dict[int, _Request] = {}
        self._slots: dict[int, asyncio.Lock] = {
            nibe.READ_TOKEN: asyncio.Lock(),
            nibe.WRITE_TOKEN: asyncio.Lock(),
        }
        self._granted = p.Subscription(0)
        self._lag_s = 0.0
        self._last_rx: float | None = None
        self._last_tx = 0.0
        self._last_health: float | None = None
        self._last_event_id = 0

    @property
    def promises(self) -> Promises:
        return Promises(fate=FateKind.EXACT, sees_other_writers=True, gateway_timestamps=True)

    # --- the session ---------------------------------------------------------------------

    async def start(self) -> None:
        """Open the session; raises HandshakeFailed (spec §13: the caller decides on fallback)."""
        loop = asyncio.get_running_loop()
        infos = await loop.getaddrinfo(self.host, None, family=socket.AF_INET)
        self._ip = str(infos[0][4][0])
        transport, _ = await loop.create_datagram_endpoint(
            lambda: _Socket(self),
            local_addr=("0.0.0.0", self.settings.local_port),  # noqa: S104  # the gateway answers here
        )
        if not isinstance(transport, asyncio.DatagramTransport):
            raise TypeError(f"expected a datagram transport, got {type(transport).__name__}")
        self._transport = transport
        await self._handshake()
        self._ticker = asyncio.create_task(self._tick())

    async def close(self) -> None:
        ticker, self._ticker = self._ticker, None
        for task in (ticker, self._rehello):
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        if self._up.is_set():
            self._send(p.Bye(id=self._new_id()))
        self._end_session(gone=False)
        if self._transport is not None:
            self._transport.close()

    async def _handshake(self) -> None:
        nonce = os.urandom(16) if self.psk is not None else None
        options = [
            p.Option.text(p.Tag.CLIENT_NAME, self.settings.name),
            p.Option.u32(p.Tag.SUBSCRIBE, self.settings.subscribe),
            p.Option.u16(p.Tag.LEASE_S, self.settings.lease_s),
            p.Option.u16(p.Tag.HEALTH_INTERVAL_S, self.settings.health_interval_s),
        ]
        if nonce is not None:
            options.append(p.Option(p.Tag.CLIENT_NONCE | p.CRITICAL, nonce))
        loop = asyncio.get_running_loop()
        for _ in range(self.settings.hello_tries):
            hello_id = self._new_id()
            waiter: asyncio.Future[bytes] = loop.create_future()
            self._hello = (hello_id, waiter)
            hello = p.Hello(id=hello_id, options=tuple(options))
            self._send_raw(p.encode(hello, key=self.psk, seq=1) if self.psk else p.encode(hello))
            try:
                data = await asyncio.wait_for(waiter, self.settings.hello_wait_s)
            except TimeoutError:
                continue
            finally:
                self._hello = None
            self._accept_welcome(data, nonce)
            return
        raise HandshakeFailed(HandshakeIssue.SILENT, f"{self.host}:{self.control_port}")

    def _accept_welcome(self, data: bytes, nonce: bytes | None) -> None:
        try:
            decoded = p.decode(data)
        except p.ProtocolError as e:
            if e.code in (p.ErrorCode.BAD_VERSION, p.ErrorCode.BAD_MAGIC):
                raise HandshakeFailed(HandshakeIssue.VERSION, str(e)) from None
            raise HandshakeFailed(HandshakeIssue.REFUSED, f"an unreadable answer: {e}") from None
        msg = decoded.message
        if isinstance(msg, p.Error):
            raise _handshake_error(msg, self.psk is not None)
        if not isinstance(msg, p.Welcome):
            raise HandshakeFailed(HandshakeIssue.REFUSED, f"{type(msg).__name__} for HELLO")
        boot = p.find(msg.options, p.Tag.BOOT_ID)
        if boot is None:
            raise HandshakeFailed(HandshakeIssue.REFUSED, "WELCOME without BOOT_ID")
        key = None
        if self.psk is not None and nonce is not None:
            gateway_nonce = p.find(msg.options, p.Tag.GATEWAY_NONCE)
            if gateway_nonce is None:
                raise HandshakeFailed(HandshakeIssue.AUTH_FAILED, "WELCOME without a nonce")
            key = p.session_key(self.psk, nonce, gateway_nonce.value, boot.as_int())
            try:
                self._recv_seq = p.verify(data, key)
            except p.ProtocolError:
                raise HandshakeFailed(
                    HandshakeIssue.AUTH_FAILED, "WELCOME isn't signed with the session key"
                ) from None
        else:
            self._recv_seq = 0
        self._key = key
        self._send_seq = 1  # HELLO used 1
        if self.boot_id != boot.as_int():
            self._clock.reset()
        self.boot_id = boot.as_int()
        t = clock.monotonic()
        self._clock.sample(msg.gw_time_us, t)
        self.welcome = msg
        granted = p.find(msg.options, p.Tag.SUBSCRIBE)
        self._granted = p.Subscription(granted.as_int() if granted else 0)
        lag = p.find(msg.options, p.Tag.TIMESTAMP_LAG_MAX_US)
        self._lag_s = (lag.as_int() / 1e6 if lag else 0.0) + DELIVERY_S
        self._last_health = t
        self._last_event_id = 0
        self._up.set()
        log.info(
            "gateway session with %s:%s, boot %08x", self.host, self.control_port, self.boot_id
        )

    def _lose_session(self, why: str, *, gone: bool) -> None:
        if self._up.is_set():
            log.warning("gateway session lost: %s", why)
        self._end_session(gone=gone)
        if self._ticker is not None:  # started: keep trying to get it back
            self._ensure_rehello()

    def _end_session(self, *, gone: bool) -> None:
        self._up.clear()
        self._key = None
        for request in list(self._requests.values()):
            _finish(request, _Final(lost=True, gone=gone))

    def _ensure_rehello(self) -> None:
        if self._rehello is None or self._rehello.done():
            self._rehello = asyncio.create_task(self._hello_again())

    async def _hello_again(self) -> None:
        while not self._up.is_set():
            try:
                await self._handshake()
            except HandshakeFailed as e:
                self.counters["hello_failures"] += 1
                log.warning("gateway session not restored: %s", e)
                await asyncio.sleep(self.settings.rehello_s)

    async def _session(self, deadline: float) -> None:
        if not self._up.is_set():
            self._ensure_rehello()
            try:
                await asyncio.wait_for(self._up.wait(), max(deadline - clock.monotonic(), 0))
            except TimeoutError:
                raise _SessionDown from None

    # --- reads ---------------------------------------------------------------------------

    async def read(
        self, register: int, *, after: float | None = None, timeout: float = 30.0
    ) -> Reading:
        deadline = clock.monotonic() + timeout
        frame = nibe.read_request(register)
        stages: list[StageEvent] = []
        why = f"no answer within {durations.text(timeout)}"
        while clock.monotonic() < deadline:
            try:
                request = await self._run(nibe.READ_TOKEN, frame, deadline)
            except _SessionDown:
                why = "no session with the gateway"
                break
            stages += request.stages
            final = request.final.result() if request.final.done() else None
            if final is None:
                self._cancel(request)
                self._requests.pop(request.id, None)
                continue
            if final.answer is not None:
                reading = self._reading(register, request, final, tuple(stages))
                if reading is None:
                    why = "the answer was for another register"
                    continue
                if after is not None and (reading.t_taken is None or reading.t_taken <= after):
                    continue
                return reading
            if final.dropped is not None:
                why = f"dropped by the gateway: {_reason(final.dropped)}"
                if final.dropped not in _RETRY_DROPS:
                    break
                await asyncio.sleep(self.settings.retry_s)
            elif final.error is not None:
                why = f"refused by the gateway: {_code(final.error)}"
                break
        raise ReadFailed(register, why, tuple(stages))

    def _reading(
        self, register: int, request: _Request, final: _Final, stages: tuple[StageEvent, ...]
    ) -> Reading | None:
        answer = final.answer
        if answer is None or answer.status != p.AnswerStatus.OK or final.t is None:
            return None
        try:
            telegram = nibe.parse_telegram(answer.frame)
        except nibe.FrameError:
            return None
        payload = telegram.payload
        if telegram.command != nibe.READ_ANSWER or len(payload) < 6:
            return None
        if int.from_bytes(payload[:2], "little") != register:
            return None
        return Reading(register, payload[2:6], request.t_sent, final.t, self._lag_s, stages)

    # --- writes --------------------------------------------------------------------------

    async def write(self, register: int, value: int, *, timeout: float = 30.0) -> WriteOutcome:
        deadline = clock.monotonic() + timeout
        frame = nibe.write_request(register, value)
        previous: list[StageEvent] = []
        """The stages of earlier attempts, refused before the pump took them."""
        current: _Request | None = None

        def outcome(result: WriteResult, why: str, t: float | None) -> WriteOutcome:
            stages = previous + (current.stages if current is not None else [])
            return WriteOutcome(register, value, result, why, t, tuple(stages))

        while True:
            try:
                current = await self._run(nibe.WRITE_TOKEN, frame, deadline)
            except _SessionDown:
                return outcome(WriteResult.NOT_TAKEN, "no session with the gateway", None)
            if not current.final.done():
                result = await self._unfinished_write(current, outcome)
                self._requests.pop(current.id, None)
                return result
            final = current.final.result()
            if final.answer is not None:
                return _write_answer(final, outcome)
            if final.nak:
                return outcome(WriteResult.NOT_TAKEN, "the pump NAKed the frame", final.t)
            if final.lost:
                if current.stage in (p.Stage.SENT, p.Stage.PUMP_ACK, p.Stage.NO_ACK_SEEN):
                    why = "taken by the pump; then the session ended"
                    return outcome(WriteResult.UNKNOWN, why, None)
                if final.gone:
                    why = "the gateway restarted with it queued"
                    return outcome(WriteResult.NOT_TAKEN, why, None)
                return outcome(WriteResult.UNKNOWN, "the session ended with it queued", None)
            if final.dropped is not None:
                if final.dropped in _RETRY_DROPS and clock.monotonic() < deadline:
                    previous += current.stages
                    current = None
                    await asyncio.sleep(self.settings.retry_s)
                    continue
                why = f"dropped by the gateway: {_reason(final.dropped)}"
                return outcome(WriteResult.NOT_TAKEN, why, final.t)
            if final.error is not None:
                why = f"refused by the gateway: {_code(final.error)}"
                return outcome(WriteResult.NOT_TAKEN, why, None)
            return outcome(WriteResult.UNKNOWN, "no result", None)

    async def _unfinished_write(self, request: _Request, outcome: _Outcome) -> WriteOutcome:
        """The deadline came first, or nothing came back. Cancel it if it may still be
        queued, and report only what is known."""
        if request.stage in (None, p.Stage.QUEUED):
            self._cancel(request)
            await wait_any(self.settings.fate_wait_s, request.final)
            if request.final.done():
                final = request.final.result()
                if final.dropped is not None:
                    why = f"not sent in time: {_reason(final.dropped)}"
                    return outcome(WriteResult.NOT_TAKEN, why, final.t)
            if request.stage is None:
                return outcome(WriteResult.UNKNOWN, "the gateway never reported on it", None)
            if request.stage is p.Stage.QUEUED:
                why = "still queued at the deadline; cancelling wasn't confirmed"
                return outcome(WriteResult.UNKNOWN, why, None)
        # It reached the pump: the gateway's answer comes, or its answer timeout does.
        answer_s = (self.settings.answer_timeout_ms / 1000 or GATEWAY_ANSWER_TIMEOUT_S) + 1.0
        await wait_any(answer_s, request.final)
        if request.final.done() and request.final.result().answer is not None:
            return _write_answer(request.final.result(), outcome)
        return outcome(WriteResult.UNKNOWN, "taken by the pump; no result came", None)

    # --- requests ------------------------------------------------------------------------

    async def _run(self, token: int, frame: bytes, deadline: float) -> _Request:
        """Send a REQUEST, and return it once it has a final result, or at the deadline."""
        await self._session(deadline)
        async with self._slots[token]:
            request = _Request(
                self._new_id(), token, frame, asyncio.get_running_loop().create_future()
            )
            self._requests[request.id] = request
            ttl_ms = int(min(max(deadline - clock.monotonic(), 0.001), 65.535) * 1000)
            self._send(
                p.Request(
                    id=request.id,
                    address=nibe.MODBUS40,
                    token=token,
                    flags=p.RequestFlag.EXPECT_ANSWER,
                    ttl_ms=max(ttl_ms, 1),
                    answer_timeout_ms=self.settings.answer_timeout_ms,
                    frame=frame,
                )
            )
            while request.stage in (None, p.Stage.QUEUED) and not request.final.done():
                remaining = deadline - clock.monotonic()
                if remaining <= 0:
                    break
                request.fated.clear()
                await wait_any(
                    min(self.settings.fate_wait_s, remaining), request.final, request.fated
                )
                if request.stage is None and not request.final.done():
                    self.counters["requests_unanswered"] += 1
                    break  # the REQUEST or its FATE was lost
        if request.stage not in (None, p.Stage.QUEUED):
            await wait_any(deadline - clock.monotonic(), request.final)
        if request.final.done():
            self._requests.pop(request.id, None)
        return request

    def _cancel(self, request: _Request) -> None:
        if self._up.is_set():
            self._send(p.Cancel(id=request.id))

    # --- what the gateway sends ----------------------------------------------------------

    def _datagram(self, data: bytes, addr: tuple[str, int]) -> None:
        if addr != (self._ip, self.control_port):
            self.counters["foreign_datagrams"] += 1
            return
        t = clock.monotonic()
        self._last_rx = t
        hello = self._hello
        try:
            decoded = p.decode(data)
        except p.ProtocolError as e:
            if hello is not None and not hello[1].done():
                hello[1].set_result(data)
            else:
                self.counters["unreadable"] += 1
                log.debug("unreadable datagram from the gateway: %s", e)
            return
        msg = decoded.message
        if (
            hello is not None
            and not hello[1].done()
            and msg.id == hello[0]
            and isinstance(msg, p.Welcome | p.Error)
        ):
            hello[1].set_result(data)
            return
        if isinstance(msg, p.Error) and decoded.seq is None and msg.code in _SESSION_GONE:
            # Unsigned: the gateway no longer knows this client, so it can't sign.
            self._lose_session(_code(msg.code), gone=True)
            return
        if self._key is not None:
            try:
                seq = p.verify(data, self._key)
            except p.ProtocolError:
                self.counters["auth_failures"] += 1
                return
            if seq <= self._recv_seq:
                self.counters["replays"] += 1
                return
            self._recv_seq = seq
        elif decoded.seq is not None:
            self.counters["unexpectedly_signed"] += 1
            return
        if not self._up.is_set():
            return
        self._clock.sample(msg.gw_time_us, t)
        match msg:
            case p.Fate():
                self._on_fate(msg)
            case p.Answer():
                request = self._requests.get(msg.id)
                if request is not None:
                    _finish(request, _Final(answer=msg, t=self._clock.to_local(msg.gw_time_us)))
            case p.Frame():
                self._count_event(msg.id)
                self._on_frame(msg)
            case p.Health():
                self._count_event(msg.id)
                self._on_health(msg, t)
            case p.Error():
                self._on_error(msg)
            case _:
                self.counters["unexpected_messages"] += 1

    def _on_fate(self, msg: p.Fate) -> None:
        request = self._requests.get(msg.id)
        if request is None:
            return
        try:
            stage = p.Stage(msg.stage)
        except ValueError:
            self.counters["unknown_stages"] += 1
            return
        t = self._clock.to_local(msg.stage_time_us)
        request.stages.append(StageEvent(stage, t, msg.detail))
        request.stage = stage
        request.fated.set()
        if stage is p.Stage.SENT:
            request.t_sent = t
        elif stage is p.Stage.DROPPED:
            _finish(request, _Final(dropped=msg.detail, t=t))
        elif stage is p.Stage.PUMP_NAK:
            _finish(request, _Final(nak=True, t=t))

    def _on_frame(self, msg: p.Frame) -> None:
        t = self._clock.to_local(msg.t_complete_us)
        try:
            origin: p.Origin | None = p.Origin(msg.origin)
        except ValueError:
            origin = None
        try:
            exchange = nibe.split_exchange(msg.data)
            telegram = nibe.parse_telegram(exchange.telegram)
        except nibe.FrameError:
            self._observers.emit(Observed(msg.data, None, b"", b"", t, origin))
            return
        self._observers.emit(
            Observed(msg.data, telegram, exchange.reply, exchange.trailer, t, origin)
        )

    def _on_health(self, msg: p.Health, t: float) -> None:
        self._last_health = t
        values: dict[str, int] = {}
        for option in msg.options:
            try:
                tag = p.Tag(option.number)
            except ValueError:
                continue  # a newer gateway's value, or an implementation's own
            if tag is p.Tag.DROPS:
                reason, count = option.unpack()
                values[f"DROPS_{_reason(reason)}"] = count
            elif tag is p.Tag.QUEUE_DEPTH:
                address, token, depth = option.unpack()
                values[f"QUEUE_DEPTH_{address:#06x}_{token:#04x}"] = depth
            else:
                try:
                    values[tag.name] = option.as_int()
                except ValueError:
                    self.counters["unreadable_health_values"] += 1
        self.gateway_values = values
        boot = values.get("BOOT_ID")
        if boot is not None and boot != self.boot_id:
            self._lose_session("the gateway restarted", gone=True)

    def _on_error(self, msg: p.Error) -> None:
        request = self._requests.get(msg.id)
        if msg.code == p.ErrorCode.UNKNOWN_REQUEST and request is not None:
            request.cancel_unknown = True
            request.fated.set()
        elif msg.code in _SESSION_GONE:
            self._lose_session(_code(msg.code), gone=True)
        elif msg.code in (p.ErrorCode.AUTH_FAILED, p.ErrorCode.REPLAY):
            self._lose_session(_code(msg.code), gone=False)
        elif request is not None:
            _finish(request, _Final(error=msg.code))
        else:
            self.counters[f"errors_{_code(msg.code)}"] += 1

    def _count_event(self, event_id: int) -> None:
        if self._last_event_id and event_id > self._last_event_id + 1:
            self.counters["events_missed"] += event_id - self._last_event_id - 1
        self._last_event_id = event_id

    # --- plumbing ------------------------------------------------------------------------

    def observe(self, callback: Callable[[Observed], None]) -> Callable[[], None]:
        return self._observers.add(callback)

    def health(self) -> LinkHealth:
        return LinkHealth(
            protocol="thermaestro-gw",
            up=self._up.is_set(),
            last_traffic=self._last_rx,
            detail={**self.counters, **self.gateway_values},
        )

    def _new_id(self) -> int:
        self._next_id = self._next_id % 0xFFFF_FFFF + 1
        return self._next_id

    def _send(self, msg: p.Message) -> None:
        if self._key is None:
            self._send_raw(p.encode(msg))
            return
        self._send_seq += 1
        self._send_raw(p.encode(msg, key=self._key, seq=self._send_seq))

    def _send_raw(self, data: bytes) -> None:
        if self._transport is None:
            raise RuntimeError("the client isn't started")
        self._transport.sendto(data, (self._ip, self.control_port))
        self._last_tx = clock.monotonic()

    async def _tick(self) -> None:
        while True:
            await asyncio.sleep(self.settings.tick_s)
            now = clock.monotonic()
            if not self._up.is_set():
                self._ensure_rehello()
                continue
            health_s = self.settings.lease_s
            if (
                p.Subscription.HEALTH in self._granted
                and self._last_health is not None
                and now - self._last_health > health_s
            ):
                self._lose_session(f"no HEALTH for {durations.text(health_s)}", gone=False)
                continue
            if now - self._last_tx >= self.settings.lease_s / 3:
                self._send(p.Keepalive(id=self._new_id()))


_RETRY_DROPS = frozenset((p.DropReason.QUEUE_FULL, p.DropReason.EVICTED, p.DropReason.SHUTDOWN))
_SESSION_GONE = frozenset((p.ErrorCode.NO_SESSION, p.ErrorCode.AUTH_REQUIRED))


def _finish(request: _Request, final: _Final) -> None:
    if not request.final.done():
        request.final.set_result(final)
    request.fated.set()


def _write_answer(final: _Final, outcome: _Outcome) -> WriteOutcome:
    answer = final.answer
    if answer is None:
        return outcome(WriteResult.UNKNOWN, "no answer", None)
    if answer.status == p.AnswerStatus.TIMEOUT:
        return outcome(WriteResult.UNKNOWN, "taken by the pump, but no 0x6C came", None)
    if answer.status == p.AnswerStatus.AMBIGUOUS:
        return outcome(
            WriteResult.UNKNOWN, "a 0x6C came with another write in flight (ambiguous)", final.t
        )
    try:
        telegram = nibe.parse_telegram(answer.frame)
    except nibe.FrameError:
        return outcome(WriteResult.UNKNOWN, "the answer didn't parse", final.t)
    if telegram.command != nibe.WRITE_ANSWER or not telegram.payload:
        return outcome(WriteResult.UNKNOWN, "the answer wasn't a 0x6C", final.t)
    byte = telegram.payload[0]
    if byte == 1:
        return outcome(WriteResult.ACCEPTED, "0x6C = 1", final.t)
    if byte == 0:
        return outcome(WriteResult.REFUSED, "0x6C = 0", final.t)
    return outcome(WriteResult.UNKNOWN, f"0x6C = {byte}", final.t)


def _handshake_error(msg: p.Error, have_key: bool) -> HandshakeFailed:
    detail = p.find(msg.options, p.Tag.ERR_DETAIL)
    text = detail.as_text() if detail else ""
    match msg.code:
        case p.ErrorCode.AUTH_REQUIRED:
            return HandshakeFailed(HandshakeIssue.AUTH_REQUIRED, text)
        case p.ErrorCode.AUTH_FAILED:
            return HandshakeFailed(HandshakeIssue.AUTH_FAILED, text)
        case p.ErrorCode.UNSUPPORTED_OPTION if have_key:
            return HandshakeFailed(HandshakeIssue.AUTH_UNSUPPORTED, text)
        case p.ErrorCode.BAD_VERSION:
            return HandshakeFailed(HandshakeIssue.VERSION, text)
    return HandshakeFailed(HandshakeIssue.REFUSED, f"{_code(msg.code)} {text}".strip())


def _reason(value: int) -> str:
    try:
        return p.DropReason(value).name
    except ValueError:
        return f"reason {value}"


def _code(value: int) -> str:
    try:
        return p.ErrorCode(value).name.lower()
    except ValueError:
        return f"error {value}"
