"""A simulated Nibe pump on a pseudo-terminal, for tests that run a gateway against it.

`PtyEnd` is the pump's end of the line, for a test that sends and expects bytes itself.
`SimPump` plays the pump's side of the MODBUS40 bus (docs/gateway-protocol.md §2): it
sends read and write tokens to MODBUS40 and ACKs a valid reply; it answers each read
request it took with a 0x6A after a delay, in the order it took them; it applies a write
and answers it with a 0x6C; and it pushes a 0x68 telegram of its LOG.SET registers now
and then.

A 0x6A carries the values as they were when the pump took the request. Whether the real
pump reads them then or when it answers isn't known; taking them early is the case in
which an answer paired with the wrong request shows an old value.
"""

import asyncio
import contextlib
import os
import time
import tty
from collections import deque
from dataclasses import dataclass, field

from thermaestro_gateway import nibe

TIMEOUT_S = 2.0
TEST_PSK = bytes(range(32))
"""The pre-shared key of the test gateways that have one."""


class PtyEnd:
    """The pump's end of a pseudo-terminal; the gateway opens the other end at `path`."""

    def __init__(self) -> None:
        self.master, self._slave = os.openpty()
        tty.setraw(self.master)
        self.path = os.ttyname(self._slave)
        self._received = bytearray()
        self._arrived = asyncio.Event()
        asyncio.get_running_loop().add_reader(self.master, self._readable)

    def _readable(self) -> None:
        self._received += os.read(self.master, 1024)
        self._arrived.set()

    def send(self, data: bytes) -> None:
        os.write(self.master, data)

    async def expect(self, n: int) -> bytes:
        async with asyncio.timeout(TIMEOUT_S):
            while len(self._received) < n:
                self._arrived.clear()
                await self._arrived.wait()
        data, self._received = bytes(self._received[:n]), self._received[n:]
        return data

    async def read_byte(self, timeout: float) -> int | None:
        """The next byte from the gateway, or None if none came in time."""
        try:
            return (await asyncio.wait_for(self.expect(1), timeout))[0]
        except TimeoutError:
            return None

    def close(self) -> None:
        asyncio.get_running_loop().remove_reader(self.master)
        os.close(self.master)
        os.close(self._slave)


def telegram(address: int, command: int, payload: bytes) -> bytes:
    """`5C ADDR CMD LEN DATA CHK`, a 0x5C in the payload doubled on the wire."""
    data = payload.replace(b"\x5c", b"\x5c\x5c")
    body = address.to_bytes(2, "big") + bytes((command, len(data))) + data
    return bytes((nibe.START_TELEGRAM,)) + body + bytes((nibe.checksum(body),))


def read_answer(register: int, values: dict[int, int]) -> bytes:
    """A 0x6A: the register, its value and the next register's, as 16-bit words."""
    payload = register.to_bytes(2, "little")
    payload += (values.get(register, 0) & 0xFFFF).to_bytes(2, "little")
    payload += (values.get(register + 1, 0) & 0xFFFF).to_bytes(2, "little")
    return telegram(nibe.MODBUS40, nibe.READ_ANSWER, payload)


def write_answer(result: int) -> bytes:
    return telegram(nibe.MODBUS40, nibe.WRITE_ANSWER, bytes((result,)))


LOG_SET = 0x68


@dataclass(eq=False)
class SimPump:
    end: PtyEnd
    registers: dict[int, int] = field(default_factory=dict)
    answer_delay_s: float = 0.05
    write_answer_delay_s: float = 0.05
    """A 0x6C quicker than a 0x6A lets a read taken before a write be answered after
    the write's result, so a read-back paired wrongly shows the old value."""
    token_interval_s: float = 0.005
    reply_wait_s: float = 0.2
    refuse: set[int] = field(default_factory=set)
    """Registers whose writes the pump refuses (0x6C = 0)."""
    not_kept: set[int] = field(default_factory=set)
    """Registers whose writes the pump accepts but doesn't keep, as some settings are."""
    log_set: list[int] = field(default_factory=list)
    log_set_interval_s: float = 0.5
    silent_reads: int = 0
    """How many of the next read requests it takes get no answer."""
    taken_reads: list[int] = field(default_factory=list)
    taken_writes: list[tuple[int, int]] = field(default_factory=list)
    _due: deque[tuple[float, bytes]] = field(default_factory=deque)
    _next_log_set: float = 0.0
    _task: asyncio.Task[None] | None = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task

    async def _run(self) -> None:
        while True:
            await self._token(nibe.READ_TOKEN)
            await self._send_due()
            await self._token(nibe.WRITE_TOKEN)
            await self._send_due()
            if self.log_set and time.monotonic() >= self._next_log_set:
                self._next_log_set = time.monotonic() + self.log_set_interval_s
                payload = b"".join(
                    r.to_bytes(2, "little")
                    + (self.registers.get(r, 0) & 0xFFFF).to_bytes(2, "little")
                    for r in self.log_set
                )
                await self._data(telegram(nibe.MODBUS40, LOG_SET, payload))
            await asyncio.sleep(self.token_interval_s)

    async def _token(self, command: int) -> None:
        self.end.send(telegram(nibe.MODBUS40, command, b""))
        first = await self.end.read_byte(self.reply_wait_s)
        if first != nibe.START_REPLY:
            return  # an ACK (nothing queued), or nobody answered
        head = await self.end.expect(2)
        rest = await self.end.expect(head[1] + 1)
        frame = bytes((first,)) + head + rest
        try:
            nibe.validate_reply(frame)
        except nibe.FrameError:
            self.end.send(bytes((nibe.NAK,)))
            return
        self.end.send(bytes((nibe.ACK,)))
        now = time.monotonic()
        register = int.from_bytes(frame[3:5], "little")
        if command == nibe.READ_TOKEN and frame[1] == nibe.READ_TOKEN:
            self.taken_reads.append(register)
            if self.silent_reads:
                self.silent_reads -= 1
            else:
                answer = read_answer(register, dict(self.registers))
                self._due.append((now + self.answer_delay_s, answer))
        elif command == nibe.WRITE_TOKEN and frame[1] == nibe.WRITE_TOKEN:
            value = int.from_bytes(frame[5:9], "little")
            self.taken_writes.append((register, value))
            accepted = register not in self.refuse
            if accepted and register not in self.not_kept:
                self.registers[register] = value
            self._due.append((now + self.write_answer_delay_s, write_answer(int(accepted))))

    async def _send_due(self) -> None:
        # Answers go out in time order; the two delays can reorder them.
        while True:
            due = sorted(self._due, key=lambda d: d[0])
            if not due or due[0][0] > time.monotonic():
                return
            self._due.remove(due[0])
            await self._data(due[0][1])

    async def _data(self, data: bytes) -> None:
        self.end.send(data)
        await self.end.read_byte(self.reply_wait_s)  # the gateway's ACK
