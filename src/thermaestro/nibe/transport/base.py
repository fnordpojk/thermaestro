"""What a Nibe bus transport offers the layers above it.

A transport moves read and write requests to the pump and reports what it can know
about them: when the pump took a request, the pump's answer, and every bus exchange the
gateway forwards. It doesn't decode values; the register map does that. Times are local
`clock.monotonic()` seconds, so they compare with each other and with the caller's own.

How much a transport can know differs: plain NibeGW says nothing
about a request, so its client infers what it can from the forwarded exchanges; the
Thermaestro gateway protocol reports each request's fate exactly.
"""

import asyncio
import contextlib
import logging
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from typing import Any, Protocol

from thermaestro_gateway import nibe
from thermaestro_gateway.protocol import Origin, Stage

log = logging.getLogger(__name__)


class FateKind(Enum):
    BEST_EFFORT = "best_effort"
    EXACT = "exact"


@dataclass(frozen=True, slots=True)
class Promises:
    fate: FateKind
    sees_other_writers: bool
    """Whether other clients' requests show in the forwarded exchanges."""
    gateway_timestamps: bool
    """Whether event times come from the gateway's clock rather than arrival here."""


@dataclass(frozen=True, slots=True)
class StageEvent:
    stage: Stage
    t: float
    detail: int = 0


@dataclass(frozen=True, slots=True)
class Reading:
    register: int
    data: bytes
    """The four bytes after the register number in the pump's 0x6A: this register's
    16-bit word, then the next register's, little-endian. The map decodes them."""
    t_taken: float | None
    """When the pump took the request this answers; None if that wasn't seen."""
    t_answered: float
    uncertainty_s: float
    """How far t_answered may be from when the answer was on the bus."""
    stages: tuple[StageEvent, ...] = ()


class WriteResult(Enum):
    ACCEPTED = "accepted"
    """The pump's 0x6C said 1. Accepted isn't applied: a pump can keep its old value,
    so read it back."""
    REFUSED = "refused"
    """The pump's 0x6C said 0."""
    NOT_TAKEN = "not_taken"
    """The pump never took the request: it was dropped, expired or NAKed."""
    UNKNOWN = "unknown"
    """It may or may not have reached the pump; `why` says what is known."""


@dataclass(frozen=True, slots=True)
class WriteOutcome:
    register: int
    value: int
    result: WriteResult
    why: str
    t_result: float | None = None
    stages: tuple[StageEvent, ...] = ()


class TransportError(Exception):
    pass


class ReadFailed(TransportError):
    def __init__(self, register: int, why: str, stages: tuple[StageEvent, ...] = ()) -> None:
        super().__init__(f"{register}: {why}")
        self.register = register
        self.why = why
        self.stages = stages


class RegisterRefused(ReadFailed):
    """The pump says it hasn't got the register (Modbus exception 2): not a passing
    failure, so it isn't asked for again."""


@dataclass(frozen=True, slots=True)
class Observed:
    """One completed bus exchange, as the gateway forwarded it."""

    data: bytes
    """The exchange's bytes, as a plain NibeGW datagram carries them."""
    telegram: nibe.Telegram | None
    """The pump's telegram; None if the bytes didn't parse."""
    reply: bytes
    """The `C0` reply that followed: the gateway's, another device's, or empty."""
    trailer: bytes
    """The byte that closed it (ACK, NAK), or empty."""
    t: float
    origin: Origin | None = None
    """Who supplied the gateway's reply, where the gateway says (the protocol's FRAME)."""


@dataclass(frozen=True, slots=True)
class LinkHealth:
    protocol: str
    up: bool
    """Whether traffic from the gateway arrives."""
    last_traffic: float | None
    detail: dict[str, int]
    """Counters: this client's own, and on the protocol the gateway's HEALTH values."""


class Transport(Protocol):
    @property
    def promises(self) -> Promises: ...

    async def read(
        self, register: int, *, after: float | None = None, timeout: float = 30.0
    ) -> Reading:
        """Read a register; with `after`, only from a request the pump took after it.
        Raises ReadFailed: a failed read never becomes a value."""
        ...

    async def write(self, register: int, value: int, *, timeout: float = 30.0) -> WriteOutcome:
        """Write a register's raw content (0 to 0xFFFFFFFF; a 16-bit register uses the
        low half). Never raises for the pump's or the gateway's behavior."""
        ...

    def observe(self, callback: Callable[[Observed], None]) -> Callable[[], None]:
        """Call `callback` for every forwarded exchange; returns the function that stops it."""
        ...

    def health(self) -> LinkHealth: ...

    async def close(self) -> None: ...


class Observers:
    """Callbacks for forwarded exchanges. One that fails is logged and the rest still run."""

    def __init__(self) -> None:
        self._callbacks: list[Callable[[Observed], None]] = []

    def add(self, callback: Callable[[Observed], None]) -> Callable[[], None]:
        self._callbacks.append(callback)

        def remove() -> None:
            with contextlib.suppress(ValueError):
                self._callbacks.remove(callback)

        return remove

    def emit(self, observed: Observed) -> None:
        for callback in list(self._callbacks):
            try:
                callback(observed)
            except Exception:
                log.exception("an observer of bus exchanges failed")


async def wait_any(
    timeout: float, future: asyncio.Future[Any] | None = None, event: asyncio.Event | None = None
) -> None:
    """Wait until `future` is done or `event` is set, or `timeout` passes. Neither is
    cancelled."""
    waiters: set[asyncio.Future[Any]] = set()
    if future is not None:
        if future.done():
            return
        waiters.add(future)
    task = None
    if event is not None:
        if event.is_set():
            return
        task = asyncio.ensure_future(event.wait())
        waiters.add(task)
    try:
        await asyncio.wait(waiters, timeout=max(timeout, 0.0), return_when=asyncio.FIRST_COMPLETED)
    finally:
        if task is not None:
            task.cancel()


def stage_after_reply(trailer: bytes) -> Stage:
    """The pump's byte after a reply: ACK, NAK, or none seen."""
    if trailer == bytes((nibe.ACK,)):
        return Stage.PUMP_ACK
    if trailer == bytes((nibe.NAK,)):
        return Stage.PUMP_NAK
    return Stage.NO_ACK_SEEN
