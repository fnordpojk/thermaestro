"""A simulated Nibe S-series pump: a small Modbus TCP server, as Nibe's S-series Modbus
document describes the pump's own.

- Input registers (function 4) and holding registers (function 3), numbered 3nnnn and
  4nnnn and asked for at address n - 1; a register it doesn't have is refused (exception 2).
- At most 20 registers in a request (exception 3 for more).
- A read of several registers answers "in reverse order": the words of the registers asked
  for, last first. A 32-bit value is kept as its high word at n and its low word at n + 1,
  so a read of both gives the low word first, as the document's example does.
- Read device identification (0x2B/0x0E) only where a test gives `identification`; no
  S-series document says what a real pump answers.
- Every request is kept, with its time, and so is every write, which is never applied.
"""

import asyncio
import struct
import time
from dataclasses import dataclass, field

MAX_PER_REQUEST = 20


@dataclass
class Request:
    unit: int
    function: int
    address: int
    count: int
    t: float


@dataclass
class SimSPump:
    registers: dict[int, int] = field(default_factory=dict)
    """16-bit words by register (30001, 40011), raw."""
    identification: dict[int, bytes] | None = None
    requests: list[Request] = field(default_factory=list)
    writes: list[bytes] = field(default_factory=list)
    silent: bool = False
    """Takes connections and answers nothing, like a pump whose Modbus is off."""
    _server: asyncio.Server | None = None

    def set32(self, register: int, value: int) -> None:
        """A 32-bit value at `register` and the next: high word first, as kept."""
        raw = value & 0xFFFF_FFFF
        self.registers[register] = raw >> 16
        self.registers[register + 1] = raw & 0xFFFF

    @property
    def port(self) -> int:
        assert self._server is not None
        return int(self._server.sockets[0].getsockname()[1])

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._serve, "127.0.0.1", 0)

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            self._server.close_clients()
            await self._server.wait_closed()

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while True:
                head = await reader.readexactly(7)
                transaction, protocol, length, unit = struct.unpack(">HHHB", head)
                pdu = await reader.readexactly(length - 1)
                if self.silent or protocol != 0:
                    continue
                answer = self._answer(unit, pdu)
                writer.write(struct.pack(">HHHB", transaction, 0, len(answer) + 1, unit) + answer)
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            writer.close()

    def _answer(self, unit: int, pdu: bytes) -> bytes:
        function = pdu[0]
        if function in (3, 4):
            address, count = struct.unpack(">HH", pdu[1:5])
            self.requests.append(Request(unit, function, address, count, time.monotonic()))
            if not 1 <= count <= MAX_PER_REQUEST:
                return bytes((function | 0x80, 3))
            base = 30_000 if function == 4 else 40_000
            wanted = [base + address + 1 + i for i in range(count)]
            if any(r not in self.registers for r in wanted):
                return bytes((function | 0x80, 2))
            words = [self.registers[r] for r in reversed(wanted)]
            return bytes((function, 2 * count)) + b"".join(w.to_bytes(2, "big") for w in words)
        if function == 0x2B and pdu[1:2] == b"\x0e":
            self.requests.append(Request(unit, function, 0, 0, time.monotonic()))
            if self.identification is None:
                return bytes((function | 0x80, 1))
            objects = b"".join(
                bytes((k, len(v))) + v for k, v in sorted(self.identification.items())
            )
            return bytes((0x2B, 0x0E, pdu[2], 0x01, 0, 0, len(self.identification))) + objects
        if function in (6, 16):
            self.writes.append(pdu)
            return bytes((function | 0x80, 1))
        return bytes((function | 0x80, 1))
