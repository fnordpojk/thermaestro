"""Modbus TCP, the S-series' own route: the pump answers on port 502 itself, no gateway.

Nibe's S-series Modbus document (TIF SV 2608) sets the rules this follows:
- input registers are read with function 4 and holding registers with function 3;
  Thermaestro numbers them 3nnnn and 4nnnn, as the pumps' own exports do, and asks for
  address n - 1, the Modbus convention the `nibe` library uses with real pumps;
- at most 20 registers in a request, and 100 a second;
- a read of several registers comes back in reverse order. Its one example is a 32-bit
  value, so one is read with two registers and its words put back. Separate registers
  are never batched: how such a batch comes back isn't documented, so each value gets
  its own request until a real pump has shown it.

Everything here is from the documents; nothing has been read on a real S-series pump yet.
Writing isn't built: Stage 2 only reads.
"""

import asyncio
import logging
from collections.abc import Callable, Collection
from typing import Any

from pymodbus.client import AsyncModbusTcpClient
from pymodbus.exceptions import ModbusException

from thermaestro import clock

from .base import (
    FateKind,
    LinkHealth,
    Observed,
    Observers,
    Promises,
    ReadFailed,
    Reading,
    RegisterRefused,
    WriteOutcome,
    WriteResult,
)

log = logging.getLogger(__name__)

MAX_PER_REQUEST = 20
PER_SECOND = 100
INPUT, HOLDING = 3, 4
ILLEGAL_DATA_ADDRESS = 2
"""The answer for a register the pump hasn't got; which it has depends on the model and
its installed accessories."""


class ModbusTransport:
    """One pump's Modbus TCP server, read one value per request."""

    def __init__(
        self,
        host: str,
        port: int = 502,
        unit: int = 1,
        *,
        wide: Collection[int] = (),
        timeout_s: float = 5.0,
    ) -> None:
        self.host = host
        self.port = port
        self.unit = unit
        self._wide = frozenset(wide)
        """The 32-bit registers, read as two."""
        self._observers = Observers()
        self._sent = b""
        self._client = AsyncModbusTcpClient(
            host, port=port, timeout=timeout_s, retries=0, trace_packet=self._traced
        )
        self._lock = asyncio.Lock()
        self._next_at = 0.0
        self._last_traffic: float | None = None
        self._counters = {"reads": 0, "failed": 0}

    async def start(self) -> None:
        if not await self._client.connect():
            raise OSError(f"{self.host}:{self.port} doesn't answer Modbus TCP")

    def _traced(self, sending: bool, data: bytes) -> bytes:
        """Each answer with the request it answers, for whoever observes (the probe's
        capture): `data` is the answer's frame and `reply` the request's, as sent."""
        if sending:
            self._sent = data
        else:
            self._last_traffic = clock.monotonic()
            self._observers.emit(Observed(data, None, self._sent, b"", clock.monotonic()))
        return data

    @property
    def promises(self) -> Promises:
        # A Modbus answer belongs to its request: what came back is known exactly.
        return Promises(FateKind.EXACT, sees_other_writers=False, gateway_timestamps=False)

    async def read(
        self, register: int, *, after: float | None = None, timeout: float = 30.0
    ) -> Reading:
        space, number = divmod(register, 10_000)
        if space not in (INPUT, HOLDING) or number < 1:
            raise ReadFailed(register, "not an S-series register (3nnnn or 4nnnn)")
        count = 2 if register in self._wide else 1
        async with self._lock:
            # A fresh request every time, so `after` is always met.
            await asyncio.sleep(max(0.0, self._next_at - clock.monotonic()))
            self._next_at = clock.monotonic() + count / PER_SECOND
            self._counters["reads"] += 1
            read = (
                self._client.read_input_registers
                if space == INPUT
                else self._client.read_holding_registers
            )
            try:
                async with asyncio.timeout(timeout):
                    answer = await read(number - 1, count=count, device_id=self.unit)
            except (ModbusException, TimeoutError, OSError) as e:
                _still_cancelled()
                self._counters["failed"] += 1
                raise ReadFailed(register, f"no answer: {e}") from None
        if answer.isError() or len(answer.registers) != count:
            self._counters["failed"] += 1
            if getattr(answer, "exception_code", None) == ILLEGAL_DATA_ADDRESS:
                raise RegisterRefused(register, "refused: the pump hasn't got it")
            raise ReadFailed(register, f"refused: {answer}")
        now = clock.monotonic()
        if count == 2:
            low, high = answer.registers  # "in reverse order": the low word first
            first, second = high, low
        else:
            first, second = answer.registers[0], 0
        data = first.to_bytes(2, "little") + second.to_bytes(2, "little")
        return Reading(register, data, now, now, 0.0)

    async def identify(self) -> dict[str, str] | None:
        """Modbus's own device identification (function 0x2B/0x0E, the basic objects:
        vendor, product code, revision), where the pump answers it; None where it doesn't.
        No S-series document mentions it, so it's shown, never relied on."""
        try:
            async with self._lock, asyncio.timeout(5.0):
                answer = await self._client.read_device_information(
                    read_code=1, object_id=0, device_id=self.unit
                )
        except (ModbusException, TimeoutError, OSError) as e:
            _still_cancelled()
            log.info("%s gave no device identification: %s", self.host, e)
            return None
        if answer.isError():
            log.info("%s refused the device identification: %s", self.host, answer)
            return None
        names = {0: "vendor", 1: "product", 2: "revision"}
        information: dict[int, Any] = getattr(answer, "information", {})
        return {
            names.get(k, str(k)): v.decode("ascii", errors="replace")
            if isinstance(v, bytes)
            else str(v)
            for k, v in sorted(information.items())
        }

    async def write(self, register: int, value: int, *, timeout: float = 30.0) -> WriteOutcome:
        return WriteOutcome(
            register, value, WriteResult.NOT_TAKEN, "writing isn't built for Modbus TCP yet"
        )

    def observe(self, callback: Callable[[Observed], None]) -> Callable[[], None]:
        return self._observers.add(callback)

    def health(self) -> LinkHealth:
        return LinkHealth(
            protocol="modbus-tcp",
            up=self._client.connected,
            last_traffic=self._last_traffic,
            detail=dict(self._counters),
        )

    async def close(self) -> None:
        self._client.close()


def _still_cancelled() -> None:
    """pymodbus answers a cancelled request with its own exception; a task being cancelled
    gets its cancellation back, so it stops instead of reading on."""
    task = asyncio.current_task()
    if task is not None and task.cancelling():
        raise asyncio.CancelledError
