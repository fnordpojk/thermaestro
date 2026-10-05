"""Request queues, fates and answer pairing (docs/gateway-protocol.md §8).

The gateway's bookkeeping, with no I/O: requests go in, the bus asks for a reply on each
token, completed exchanges come back, and the fates and answers owed to protocol clients
collect in an outbox.

- One queue per (address, token), shared by plain NibeGW and protocol requests. Plain
  requests behave as on esphome-nibe: a full queue drops its oldest entry. Protocol
  requests are refused when the queue is full, and a protocol request a plain one pushes
  out is reported as EVICTED.
- Reads are paired with the pump's 0x6A answers first in, first out per register: the
  read requests the pump took, from every client and plain ones included, wait in the
  order it took them, and an answer belongs to the oldest. A read-back after a write
  thus never gets the answer to a request taken before the write.
- Writes are paired with 0x6C answers by order. A protocol write isn't sent while another
  write is awaiting its 0x6C. If a 0x6C arrives while more than one write is in flight
  (a plain client's writes can overlap), the pairing is reported as AMBIGUOUS.
"""

from collections import Counter, deque
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

from thermaestro_gateway import nibe
from thermaestro_gateway import protocol as p
from thermaestro_gateway.bus import Exchange, Reply

DEFAULT_QUEUE_CAP = 3
DEFAULT_ANSWER_TIMEOUT_US = 5_000_000

Key = tuple[int, int]


@dataclass(eq=False, slots=True)
class Queued:
    frame: bytes
    key: Key
    client: object | None = None
    """None for a plain NibeGW request, or once its protocol client has gone."""
    request_id: int = 0
    deadline_us: int | None = None
    expect_answer: bool = False
    answer_timeout_us: int = DEFAULT_ANSWER_TIMEOUT_US
    sent_us: int | None = None

    @property
    def is_write(self) -> bool:
        return self.key == (nibe.MODBUS40, nibe.WRITE_TOKEN)

    @property
    def is_read(self) -> bool:
        return self.key == (nibe.MODBUS40, nibe.READ_TOKEN)

    @property
    def register(self) -> int:
        return int.from_bytes(self.frame[3:5], "little")


@dataclass(frozen=True, slots=True)
class Outgoing:
    client: object
    message: p.Fate | p.Answer


@dataclass(slots=True)
class EngineStats:
    drops: Counter[p.DropReason] = field(default_factory=Counter)
    evictions: int = 0
    ambiguous_answers: int = 0
    answer_timeouts: int = 0


class Engine:
    def __init__(
        self,
        keys: Iterable[Key],
        *,
        queue_cap: int = DEFAULT_QUEUE_CAP,
        constants: Mapping[Key, bytes] | None = None,
        default_answer_timeout_us: int = DEFAULT_ANSWER_TIMEOUT_US,
    ) -> None:
        self.queue_cap = queue_cap
        self.default_answer_timeout_us = default_answer_timeout_us
        self.stats = EngineStats()
        self._queues: dict[Key, deque[Queued]] = {k: deque() for k in keys}
        self._constants = {
            key: nibe.reply_frame(key[1], data) for key, data in (constants or {}).items()
        }
        self._reads: dict[int, deque[Queued]] = {}
        """Per register, the reads the pump took, oldest first."""
        self._writes: deque[Queued] = deque()
        self._outbox: list[Outgoing] = []

    # --- requests in ---------------------------------------------------------------------

    def submit(self, client: object, request: p.Request, now_us: int) -> None:
        key = (request.address, request.token)
        reason = self._invalid(request.frame, key)
        if reason is None and len(self._queues[key]) >= self.queue_cap:
            reason = p.DropReason.QUEUE_FULL
        if reason is not None:
            self._drop_fate(client, request.id, reason, now_us)
            return
        entry = Queued(
            frame=request.frame,
            key=key,
            client=client,
            request_id=request.id,
            deadline_us=now_us + request.ttl_ms * 1000 if request.ttl_ms else None,
            expect_answer=bool(request.flags & p.RequestFlag.EXPECT_ANSWER),
            answer_timeout_us=(
                request.answer_timeout_ms * 1000
                if request.answer_timeout_ms
                else self.default_answer_timeout_us
            ),
        )
        queue = self._queues[key]
        if request.flags & p.RequestFlag.PRIORITY:
            queue.appendleft(entry)
            ahead = 0
        else:
            queue.append(entry)
            ahead = len(queue) - 1
        self._fate(entry, p.Stage.QUEUED, now_us, detail=ahead)

    def submit_plain(self, address: int, token: int, frame: bytes, now_us: int) -> bool:
        """Queue a plain NibeGW request; False if it was invalid or has no queue."""
        key = (address, token)
        if self._invalid(frame, key) is not None:
            return False
        queue = self._queues[key]
        if len(queue) >= self.queue_cap:
            oldest = queue.popleft()
            if oldest.client is not None:
                self.stats.evictions += 1
                self._drop_fate(oldest.client, oldest.request_id, p.DropReason.EVICTED, now_us)
        queue.append(Queued(frame=frame, key=key, answer_timeout_us=self.default_answer_timeout_us))
        return True

    def cancel(self, client: object, request_id: int, now_us: int) -> bool:
        for queue in self._queues.values():
            for entry in queue:
                if entry.client == client and entry.request_id == request_id:
                    queue.remove(entry)
                    self._drop_fate(client, request_id, p.DropReason.CANCELLED, now_us)
                    return True
        return False

    def forget(self, client: object) -> None:
        """A client's session ended: its queued requests never reach the pump, and nothing
        more is reported to it. Reads and writes already taken keep their places, for the
        pairing of others."""
        for queue in self._queues.values():
            for entry in [e for e in queue if e.client == client]:
                queue.remove(entry)
        for entry in self._taken():
            if entry.client == client:
                entry.client = None

    def shutdown(self, now_us: int) -> None:
        """Drop every queued request, telling protocol clients why."""
        for queue in self._queues.values():
            for entry in queue:
                if entry.client is not None:
                    self._drop_fate(entry.client, entry.request_id, p.DropReason.SHUTDOWN, now_us)
            queue.clear()

    # --- the bus -------------------------------------------------------------------------

    def reply_for(self, address: int, command: int, now_us: int) -> Reply | None:
        key = (address, command)
        queue = self._queues.get(key)
        if queue:
            for entry in list(queue):
                if entry.deadline_us is not None and now_us >= entry.deadline_us:
                    queue.remove(entry)
                    if entry.client is not None:
                        self._drop_fate(
                            entry.client, entry.request_id, p.DropReason.EXPIRED, now_us
                        )
                    continue
                if entry.client is not None and entry.is_write and self._writes:
                    continue  # one protocol write in flight at a time
                queue.remove(entry)
                entry.sent_us = now_us
                self._fate(entry, p.Stage.SENT, now_us)
                return Reply(entry.frame, ref=entry)
        constant = self._constants.get(key)
        return Reply(constant) if constant is not None else None

    def on_exchange(self, exchange: Exchange) -> None:
        now = exchange.t_complete_us
        entry = exchange.reply.ref if exchange.reply is not None else None
        if isinstance(entry, Queued):
            stage = {
                nibe.ACK: p.Stage.PUMP_ACK,
                nibe.NAK: p.Stage.PUMP_NAK,
            }.get(exchange.trailer if exchange.trailer is not None else -1, p.Stage.NO_ACK_SEEN)
            self._fate(entry, stage, now)
            if stage is not p.Stage.PUMP_NAK:
                if entry.is_write:
                    self._writes.append(entry)
                elif entry.is_read:
                    self._reads.setdefault(entry.register, deque()).append(entry)
        telegram = exchange.telegram
        if telegram is None or telegram.address != nibe.MODBUS40:
            return
        raw = exchange.data[: nibe.telegram_length(exchange.data)]
        if telegram.command == nibe.READ_ANSWER and len(telegram.payload) >= 2:
            register = int.from_bytes(telegram.payload[:2], "little")
            waiting = self._reads.get(register)
            # One that should have been answered by now lost its answer; this one isn't it.
            while waiting and self._overdue(waiting[0], now):
                self._timeout(waiting.popleft(), now)
            if waiting:
                read = waiting.popleft()
                if read.client is not None and read.expect_answer:
                    self._answer(read, p.AnswerStatus.OK, raw, now)
            if not waiting:
                self._reads.pop(register, None)
        elif telegram.command == nibe.WRITE_ANSWER and self._writes:
            ambiguous = len(self._writes) > 1
            written = self._writes.popleft()
            if written.client is not None and written.expect_answer:
                if ambiguous:
                    self.stats.ambiguous_answers += 1
                status = p.AnswerStatus.AMBIGUOUS if ambiguous else p.AnswerStatus.OK
                self._answer(written, status, raw, now)

    def tick(self, now_us: int) -> None:
        """Expire queued requests and give up on answers that didn't come."""
        for queue in self._queues.values():
            for entry in [
                e for e in queue if e.deadline_us is not None and now_us >= e.deadline_us
            ]:
                queue.remove(entry)
                if entry.client is not None:
                    self._drop_fate(entry.client, entry.request_id, p.DropReason.EXPIRED, now_us)
        for register, waiting in list(self._reads.items()):
            for read in [r for r in waiting if self._overdue(r, now_us)]:
                waiting.remove(read)
                self._timeout(read, now_us)
            if not waiting:
                del self._reads[register]
        for written in [w for w in self._writes if self._overdue(w, now_us)]:
            self._writes.remove(written)
            self._timeout(written, now_us)

    # --- out -----------------------------------------------------------------------------

    def take_outbox(self) -> list[Outgoing]:
        out, self._outbox = self._outbox, []
        return out

    def depths(self) -> dict[Key, int]:
        return {key: len(queue) for key, queue in self._queues.items()}

    @staticmethod
    def owner_of(reply: Reply) -> tuple[object, int] | None:
        entry = reply.ref
        if isinstance(entry, Queued) and entry.client is not None:
            return entry.client, entry.request_id
        return None

    # --- helpers -------------------------------------------------------------------------

    def _invalid(self, frame: bytes, key: Key) -> p.DropReason | None:
        try:
            nibe.validate_reply(frame)
        except nibe.FrameError:
            return p.DropReason.INVALID_FRAME
        if frame[1] != key[1]:
            return p.DropReason.TOKEN_MISMATCH
        if key not in self._queues:
            return p.DropReason.UNKNOWN_KEY
        return None

    @staticmethod
    def _overdue(entry: Queued, now_us: int) -> bool:
        return entry.sent_us is not None and now_us >= entry.sent_us + entry.answer_timeout_us

    def _fate(self, entry: Queued, stage: p.Stage, now_us: int, detail: int = 0) -> None:
        if entry.client is not None:
            self._outbox.append(
                Outgoing(
                    entry.client,
                    p.Fate(
                        id=entry.request_id,
                        stage=stage,
                        detail=detail,
                        stage_time_us=now_us,
                        gw_time_us=now_us,
                    ),
                )
            )

    def _drop_fate(
        self, client: object, request_id: int, reason: p.DropReason, now_us: int
    ) -> None:
        self.stats.drops[reason] += 1
        self._outbox.append(
            Outgoing(
                client,
                p.Fate(
                    id=request_id,
                    stage=p.Stage.DROPPED,
                    detail=reason,
                    stage_time_us=now_us,
                    gw_time_us=now_us,
                ),
            )
        )

    def _answer(self, entry: Queued, status: p.AnswerStatus, frame: bytes, now_us: int) -> None:
        if entry.client is not None:
            self._outbox.append(
                Outgoing(
                    entry.client,
                    p.Answer(id=entry.request_id, status=status, frame=frame, gw_time_us=now_us),
                )
            )

    def _timeout(self, entry: Queued, now_us: int) -> None:
        """No answer came in time; only a protocol client that asked for one hears of it."""
        if entry.client is not None and entry.expect_answer:
            self.stats.answer_timeouts += 1
            self._answer(entry, p.AnswerStatus.TIMEOUT, b"", now_us)

    def _taken(self) -> Iterable[Queued]:
        for waiting in self._reads.values():
            yield from waiting
        yield from self._writes
