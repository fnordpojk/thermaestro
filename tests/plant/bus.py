"""The pump's bus, in process: a transport for the Nibe plugin that takes its requests to
the plant as the frames a gateway would send, and answers with the pump's telegrams.

Every request and answer is encoded and parsed by the same code the gateway and the bus
tests use, so behavior tests stay below the codec; there is no pty, no gateway and no
socket, and its delays are simulated time. One request is on the bus at a time, taken and
answered about as fast as on a real pump. Unasked, the pump pushes its LOG.SET registers
and its product information now and then.
"""

import asyncio
import contextlib
from collections.abc import Callable
from typing import Any, Protocol

from simpump import answer_with_words, read_answer, telegram, write_answer
from thermaestro_gateway import nibe
from thermaestro_gateway.protocol import Origin

from thermaestro import clock
from thermaestro.nibe import profile
from thermaestro.nibe.transport.base import (
    FateKind,
    LinkHealth,
    Observed,
    Observers,
    Promises,
    Reading,
    Transport,
    WriteOutcome,
    WriteResult,
)

LOG_SET = 0x68
PRODUCT_INFO = 0x6D
WORD_SWAP = 48852


class Pump(Protocol):
    """What the bus needs of the plant: its registers, and writes to them."""

    registers: dict[int, int]
    registers32: dict[int, int]

    def write(self, register: int, value: int) -> bool: ...


class SimBus:
    def __init__(
        self,
        pump: Pump,
        *,
        take_s: float = 0.5,
        answer_s: float = 0.3,
        log_set_s: float = 10.0,
        info_s: float = 60.0,
        product: str = "F1245-6 CU",
    ) -> None:
        self.pump = pump
        self.take_s = take_s
        self.answer_s = answer_s
        self.log_set_s = log_set_s
        self.info_s = info_s
        self.product = product
        self.writes: list[tuple[float, int, int]] = []
        """Every write the pump took: when (seconds since the epoch), the register, the value."""
        self.reads = 0
        self._observers = Observers()
        self._lock = asyncio.Lock()
        self._task: asyncio.Task[None] | None = None
        self._last: float | None = None

    async def connect(self, config: Any, **settings: Any) -> Transport:
        """What the plugin calls to connect: this bus, started."""
        if self._task is None:
            self._task = asyncio.create_task(self._unasked())
        return self

    # --- the transport ---------------------------------------------------------------------

    @property
    def promises(self) -> Promises:
        return Promises(FateKind.EXACT, sees_other_writers=True, gateway_timestamps=False)

    async def read(
        self, register: int, *, after: float | None = None, timeout: float = 30.0
    ) -> Reading:
        frame = nibe.read_request(register)
        nibe.validate_reply(frame)
        async with self._lock:
            await asyncio.sleep(self.take_s)
            taken = clock.monotonic()
            answer = self._answer(int.from_bytes(frame[3:5], "little"))
            await asyncio.sleep(self.answer_s)
        payload = nibe.parse_telegram(answer).payload
        self.reads += 1
        self._last = clock.monotonic()
        return Reading(register, payload[2:6], taken, self._last, 0.0)

    async def write(self, register: int, value: int, *, timeout: float = 30.0) -> WriteOutcome:
        frame = nibe.write_request(register, value)
        nibe.validate_reply(frame)
        async with self._lock:
            await asyncio.sleep(self.take_s)
            accepted = self._take(frame, Origin.THIS_CLIENT)
            answer = write_answer(int(accepted))
            await asyncio.sleep(self.answer_s)
        result = nibe.parse_telegram(answer).payload[0]
        self._last = clock.monotonic()
        return WriteOutcome(
            register,
            value,
            WriteResult.ACCEPTED if result == 1 else WriteResult.REFUSED,
            f"0x6C = {result}",
            self._last,
        )

    def observe(self, callback: Callable[[Observed], None]) -> Callable[[], None]:
        return self._observers.add(callback)

    def health(self) -> LinkHealth:
        return LinkHealth(
            "sim", True, self._last, {"reads": self.reads, "writes": len(self.writes)}
        )

    async def close(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    # --- another client --------------------------------------------------------------------

    def foreign_write(
        self, register: int, value: int, origin: Origin = Origin.PLAIN_CLIENT
    ) -> None:
        """Another client on the gateway writes, as NibePi does beside Thermaestro."""
        self._take(nibe.write_request(register, value), origin)

    # --- the pump's side -------------------------------------------------------------------

    def _take(self, frame: bytes, origin: Origin) -> bool:
        register = int.from_bytes(frame[3:5], "little")
        value = int.from_bytes(frame[5:9], "little")
        accepted = self.pump.write(register, value)
        self.writes.append((clock.time(), register, value))
        token = telegram(nibe.MODBUS40, nibe.WRITE_TOKEN, b"")
        trailer = bytes((nibe.ACK,))
        self._observers.emit(
            Observed(
                token + frame + trailer,
                nibe.parse_telegram(token),
                frame,
                trailer,
                clock.monotonic(),
                origin,
            )
        )
        return accepted

    def _answer(self, register: int) -> bytes:
        if register not in self.pump.registers32:
            return read_answer(register, self.pump.registers)
        value = self.pump.registers32[register] & 0xFFFF_FFFF
        high, low = value >> 16, value & 0xFFFF
        if self.pump.registers.get(WORD_SWAP, 1) == 0:
            return answer_with_words(register, high, low)
        return answer_with_words(register, low, high)

    def _push(self, command: int, payload: bytes) -> None:
        data = telegram(nibe.MODBUS40, command, payload)
        self._observers.emit(Observed(data, nibe.parse_telegram(data), b"", b"", clock.monotonic()))

    async def _unasked(self) -> None:
        firmware = self.pump.registers.get(43001, 9721)
        info = b"\x01" + firmware.to_bytes(2, "big") + self.product.encode("ascii")
        next_info = 0.0
        while True:
            if clock.monotonic() >= next_info:
                self._push(PRODUCT_INFO, info)
                next_info = clock.monotonic() + self.info_s
            payload = b"".join(
                r.to_bytes(2, "little")
                + (self.pump.registers.get(r, 0) & 0xFFFF).to_bytes(2, "little")
                for r in profile.LOG_SET
            )
            self._push(LOG_SET, payload)
            await asyncio.sleep(self.log_set_s)
