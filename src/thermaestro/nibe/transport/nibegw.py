"""Plain NibeGW over UDP, as esphome-nibe and openHAB's NibeGW speak it.

The bus and plain NibeGW: docs/gateway-protocol.md §2.

A request is a `C0` reply frame sent to the gateway's read or write port. The gateway
queues it and hands it to the pump at the next matching token. What comes back is every
completed bus exchange, sent to every client heard from in the last 120 s. Nothing says
what became of a request, so this client follows the exchanges and pairs answers itself,
by a method proven against a real pump:

- one socket, so the gateway counts this client once (a client opening a socket
  per request has exhausted an ESP32 gateway's buffers);
- a request is sent again only while it hasn't been seen on the bus;
- 0x6A answers are paired first in, first out with the read requests the pump took, per
  register. A read-back first waits until the register has been quiet for longer than
  the pump takes to answer, so no request taken before it is still unanswered;
- a 0x6C belongs to the oldest write the pump took that hasn't had one.

A gateway that forwards only the pump's telegrams (openHAB's NibeGW) shows no request
being taken: reads still work, a write is sent once, and its result is unknown.
"""

import asyncio
import contextlib
import logging
import socket
from collections import Counter, deque
from collections.abc import Callable
from dataclasses import dataclass, field

from thermaestro_gateway import nibe
from thermaestro_gateway.protocol import Stage

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
    WriteOutcome,
    WriteResult,
    stage_after_reply,
    wait_any,
)

log = logging.getLogger(__name__)

LAG_S = 0.05
"""How long after its last byte an exchange may arrive here: the gateway's processing
(ESPHome: up to about 16 ms, docs/gateway-protocol.md §9) plus delivery on the LAN."""


@dataclass(frozen=True, slots=True)
class PlainSettings:
    local_port: int = 0
    """0 picks a free port. A gateway with a fixed target (openHAB's nibegw.c) needs it set."""
    resend_s: float = 10.0
    """Send a request again if it hasn't been seen on the bus by then."""
    answer_s: float = 5.0
    """Longer than the pump takes to answer (~1 s): a 0x6A or 0x6C this late was lost, and
    a register quiet this long has no request left unanswered."""
    silent_s: float = 10.0
    """No datagram for this long: the gateway isn't sending to this client."""
    keepalive_s: float = 60.0
    """esphome-nibe forgets a client after 120 s without a request; one comes before that."""
    keepalive_register: int = 40004
    """Read to stay a target when nothing else is asked: BT1, on every F-series pump."""
    tick_s: float = 1.0


@dataclass(eq=False, slots=True)
class _Read:
    register: int
    frame: bytes
    after: float | None
    answer: asyncio.Future[Reading]
    delivered: asyncio.Event = field(default_factory=asyncio.Event)
    taken: bool = False
    stages: list[StageEvent] = field(default_factory=list)


@dataclass(eq=False, slots=True)
class _Write:
    frame: bytes
    result: asyncio.Future[tuple[int, float]]
    """The 0x6C's byte, and when it came."""
    delivered: asyncio.Event = field(default_factory=asyncio.Event)
    taken: bool = False
    stages: list[StageEvent] = field(default_factory=list)


@dataclass(eq=False, slots=True)
class _TakenWrite:
    t: float
    mine: _Write | None


class _Socket(asyncio.DatagramProtocol):
    def __init__(self, client: "PlainClient") -> None:
        self.client = client

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        self.client._datagram(data, addr)

    def error_received(self, exc: Exception) -> None:
        self.client.counters["udp_errors"] += 1
        log.warning("NibeGW socket: %s", exc)


class PlainClient:
    def __init__(
        self,
        host: str,
        read_port: int = 9999,
        write_port: int = 10000,
        *,
        settings: PlainSettings | None = None,
    ) -> None:
        self.host = host
        self.read_port = read_port
        self.write_port = write_port
        self.settings = settings or PlainSettings()
        self.counters: Counter[str] = Counter()
        self._observers = Observers()
        self._ip = ""
        self._transport: asyncio.DatagramTransport | None = None
        self._ticker: asyncio.Task[None] | None = None
        self._reads: list[_Read] = []
        self._read_slot = asyncio.Lock()
        """Held while one of this client's reads waits in the gateway's queue."""
        self._write: _Write | None = None
        self._write_lock = asyncio.Lock()
        self._taken_reads: dict[int, deque[float]] = {}
        self._taken_writes: deque[_TakenWrite] = deque()
        self._activity: dict[int, float] = {}
        self._sees_replies = False
        self._receiving_since: float | None = None
        self._last_rx: float | None = None
        self._last_tx = 0.0

    @property
    def promises(self) -> Promises:
        return Promises(
            fate=FateKind.BEST_EFFORT,
            sees_other_writers=self._sees_replies,
            gateway_timestamps=False,
        )

    async def start(self) -> None:
        loop = asyncio.get_running_loop()
        infos = await loop.getaddrinfo(self.host, None, family=socket.AF_INET)
        self._ip = str(infos[0][4][0])
        transport, _ = await loop.create_datagram_endpoint(
            lambda: _Socket(self),
            local_addr=("0.0.0.0", self.settings.local_port),  # noqa: S104  # replies come from the gateway
        )
        if not isinstance(transport, asyncio.DatagramTransport):
            raise TypeError(f"expected a datagram transport, got {type(transport).__name__}")
        self._transport = transport
        self._ticker = asyncio.create_task(self._tick())

    async def close(self) -> None:
        if self._ticker is not None:
            self._ticker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._ticker
        if self._transport is not None:
            self._transport.close()

    def observe(self, callback: Callable[[Observed], None]) -> Callable[[], None]:
        return self._observers.add(callback)

    def health(self) -> LinkHealth:
        return LinkHealth(
            protocol="nibegw",
            up=self._receiving_since is not None,
            last_traffic=self._last_rx,
            detail=dict(self.counters),
        )

    # --- reads ---------------------------------------------------------------------------

    async def read(
        self, register: int, *, after: float | None = None, timeout: float = 30.0
    ) -> Reading:
        deadline = clock.monotonic() + timeout
        frame = nibe.read_request(register)
        if after is not None:
            await self._settle(register, deadline)
        pending = _Read(register, frame, after, asyncio.get_running_loop().create_future())
        self._reads.append(pending)
        try:
            while not pending.answer.done():
                if clock.monotonic() >= deadline:
                    raise ReadFailed(
                        register,
                        f"no answer within {durations.text(timeout)}",
                        tuple(pending.stages),
                    )
                async with self._read_slot:
                    pending.delivered.clear()
                    self._send(frame, self.read_port)
                    await wait_any(
                        min(self.settings.resend_s, deadline - clock.monotonic()),
                        pending.answer,
                        pending.delivered,
                    )
                if pending.delivered.is_set() and pending.taken:
                    await wait_any(
                        min(self.settings.answer_s, deadline - clock.monotonic()), pending.answer
                    )
                if not pending.answer.done():
                    self.counters["read_resends"] += 1
        finally:
            self._reads.remove(pending)
        return pending.answer.result()

    async def _settle(self, register: int, deadline: float) -> None:
        """Wait until nothing has happened to `register` for answer_s while this client was
        receiving, then pair it afresh: every request taken before has been answered."""
        while True:
            now = clock.monotonic()
            if self._receiving_since is not None:
                since = max(self._receiving_since, self._activity.get(register, 0.0))
                if now - since >= self.settings.answer_s:
                    self._taken_reads.pop(register, None)
                    return
            elif now - self._last_tx >= self.settings.resend_s:
                self._send(nibe.read_request(self.settings.keepalive_register), self.read_port)
            if now >= deadline:
                raise ReadFailed(register, "the register was never quiet long enough to pair")
            await asyncio.sleep(min(0.05, self.settings.answer_s / 4))

    # --- writes --------------------------------------------------------------------------

    async def write(self, register: int, value: int, *, timeout: float = 30.0) -> WriteOutcome:
        deadline = clock.monotonic() + timeout
        frame = nibe.write_request(register, value)

        def outcome(result: WriteResult, why: str, t: float | None = None) -> WriteOutcome:
            return WriteOutcome(register, value, result, why, t, tuple(pending.stages))

        async with self._write_lock:
            pending = _Write(frame, asyncio.get_running_loop().create_future())
            self._write = pending
            try:
                sends = 0
                while not pending.delivered.is_set() and clock.monotonic() < deadline:
                    if sends and not self._sees_replies:
                        break  # it can't be seen whether it was taken; again could write twice
                    self._send(frame, self.write_port)
                    sends += 1
                    await wait_any(
                        min(self.settings.resend_s, deadline - clock.monotonic()),
                        event=pending.delivered,
                    )
                if not pending.delivered.is_set():
                    if not self._sees_replies:
                        return outcome(
                            WriteResult.UNKNOWN,
                            "the gateway doesn't forward what it sends to the pump",
                        )
                    return outcome(
                        WriteResult.UNKNOWN,
                        f"not seen on the bus within {durations.text(timeout)};"
                        " the gateway may still send it",
                    )
                if not pending.taken:
                    return outcome(WriteResult.NOT_TAKEN, "the pump NAKed the frame")
                await wait_any(
                    min(self.settings.answer_s, deadline - clock.monotonic()), pending.result
                )
                if not pending.result.done():
                    return outcome(WriteResult.UNKNOWN, "taken by the pump, but no 0x6C came")
                byte, t = pending.result.result()
                if byte == 1:
                    return outcome(WriteResult.ACCEPTED, "0x6C = 1", t)
                if byte == 0:
                    return outcome(WriteResult.REFUSED, "0x6C = 0", t)
                return outcome(WriteResult.UNKNOWN, f"0x6C = {byte}", t)
            finally:
                self._write = None

    # --- what the gateway forwards -------------------------------------------------------

    def _datagram(self, data: bytes, addr: tuple[str, int]) -> None:
        if addr[0] != self._ip:
            self.counters["foreign_datagrams"] += 1
            return
        t = clock.monotonic()
        self._last_rx = t
        if self._receiving_since is None:
            self._receiving_since = t
        try:
            exchange = nibe.split_exchange(data)
            telegram = nibe.parse_telegram(exchange.telegram)
        except nibe.FrameError:
            self.counters["unparsed"] += 1
            self._observers.emit(Observed(data, None, b"", b"", t))
            return
        if telegram.address == nibe.MODBUS40:
            self._follow(telegram, exchange, t)
        self._observers.emit(Observed(data, telegram, exchange.reply, exchange.trailer, t))

    def _follow(self, telegram: nibe.Telegram, exchange: nibe.Exchange, t: float) -> None:
        reply = exchange.reply
        if telegram.is_token:
            if exchange.trailer or reply:
                self._sees_replies = True
            if not reply or reply[1] != telegram.command:
                return
            stage = stage_after_reply(exchange.trailer)
            taken = stage is not Stage.PUMP_NAK
            if telegram.command == nibe.READ_TOKEN and len(reply) == 6:
                register = int.from_bytes(reply[3:5], "little")
                self._activity[register] = t
                if taken:
                    self._taken_reads.setdefault(register, deque()).append(t)
                for read in self._reads:
                    if read.frame == reply and not read.delivered.is_set():
                        read.stages += [StageEvent(Stage.SENT, t), StageEvent(stage, t)]
                        read.taken = taken
                        read.delivered.set()
                        break
            elif telegram.command == nibe.WRITE_TOKEN:
                mine = self._write
                if mine is not None and (mine.frame != reply or mine.delivered.is_set()):
                    mine = None
                if mine is not None:
                    mine.stages += [StageEvent(Stage.SENT, t), StageEvent(stage, t)]
                    mine.taken = taken
                    mine.delivered.set()
                if taken:
                    self._taken_writes.append(_TakenWrite(t, mine))
        elif telegram.command == nibe.READ_ANSWER and len(telegram.payload) >= 6:
            self._answered_read(telegram.payload, t)
        elif telegram.command == nibe.WRITE_ANSWER and telegram.payload:
            self._answered_write(telegram.payload[0], t)

    def _answered_read(self, payload: bytes, t: float) -> None:
        register = int.from_bytes(payload[:2], "little")
        self._activity[register] = t
        waiting = self._taken_reads.get(register)
        while waiting and t - waiting[0] > self.settings.answer_s:
            waiting.popleft()  # its answer was lost
        taken_at = waiting.popleft() if waiting else None
        for read in self._reads:
            if read.register != register or read.answer.done():
                continue
            if read.after is not None and (taken_at is None or taken_at <= read.after):
                continue
            read.answer.set_result(
                Reading(register, payload[2:6], taken_at, t, LAG_S, tuple(read.stages))
            )

    def _answered_write(self, byte: int, t: float) -> None:
        while self._taken_writes and t - self._taken_writes[0].t > self.settings.answer_s:
            self._taken_writes.popleft()
        if not self._taken_writes:
            self.counters["unpaired_write_answers"] += 1
            return
        mine = self._taken_writes.popleft().mine
        if mine is not None and not mine.result.done():
            mine.result.set_result((byte, t))

    # --- helpers -------------------------------------------------------------------------

    def _send(self, frame: bytes, port: int) -> None:
        if self._transport is None:
            raise RuntimeError("the client isn't started")
        self._transport.sendto(frame, (self._ip, port))
        self._last_tx = clock.monotonic()

    async def _tick(self) -> None:
        while True:
            await asyncio.sleep(self.settings.tick_s)
            now = clock.monotonic()
            if self._last_rx is not None and now - self._last_rx > self.settings.silent_s:
                self._receiving_since = None
            if now - self._last_tx >= self.settings.keepalive_s:
                self._send(nibe.read_request(self.settings.keepalive_register), self.read_port)
