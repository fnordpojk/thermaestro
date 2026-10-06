"""A simulated Nibe pump on a pseudo-terminal, for tests that run a gateway against it.

`PtyEnd` is the pump's end of the line, for a test that sends and expects bytes itself.
`SimPump` plays the pump's side of the MODBUS40 bus (docs/gateway-protocol.md §2): it
sends read and write tokens to MODBUS40 and ACKs a valid reply; it answers each read
request it took with a 0x6A after a delay, in the order it took them; it applies a write
and answers it with a 0x6C; it pushes a 0x68 telegram of its LOG.SET registers now and
then; and it sends its product information (0x6D) and a 0x6E every 15 s, 7 s apart.

A 0x6A carries the values as they were when the pump took the request. Whether the real
pump reads them then or when it answers isn't known; taking them early is the case in
which an answer paired with the wrong request shows an old value.

`OtherClient` is another program on the same gateway, as NibePi runs beside Thermaestro.
"""

import asyncio
import contextlib
import os
import random
import time
import tty
from collections import Counter, deque
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
    return answer_with_words(
        register, values.get(register, 0) & 0xFFFF, values.get(register + 1, 0) & 0xFFFF
    )


def answer_with_words(register: int, first: int, second: int) -> bytes:
    payload = register.to_bytes(2, "little") + first.to_bytes(2, "little")
    return telegram(nibe.MODBUS40, nibe.READ_ANSWER, payload + second.to_bytes(2, "little"))


def write_answer(result: int) -> bytes:
    return telegram(nibe.MODBUS40, nibe.WRITE_ANSWER, bytes((result,)))


LOG_SET = 0x68
PRODUCT_INFO = 0x6D
MODBUS_ADDRESS = 0x6E
"""Sent every 15 s with payload 01 and answered with an ACK; its meaning is undocumented."""
WORD_SWAP = 48852
"""Modbus40 Word Swap: 0 puts a 32-bit value's high word first on the bus, 1 (the factory
setting) the low word."""


def _signed16(word: int) -> int:
    return word - 0x1_0000 if word & 0x8000 else word


@dataclass(eq=False)
class SimPump:
    end: PtyEnd
    registers: dict[int, int] = field(default_factory=dict)
    """16-bit registers, by their raw word."""
    registers32: dict[int, int] = field(default_factory=dict)
    """32-bit registers, answered in the word order 48852 sets."""
    one_word_twice: set[int] = field(default_factory=lambda: {40940})
    """32-bit registers whose answer carries one word twice, the one 48852 puts first, as
    40940 (degree minutes) does."""
    absent: set[int] = field(default_factory=set)
    """Registers whose requests the pump takes but never answers, as some it doesn't have."""
    clamp: dict[int, tuple[int, int]] = field(default_factory=dict)
    """Registers whose writes are accepted and kept clamped to these signed limits."""
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
    product: str = "F1245-6 CU"
    firmware: int = 9721
    info_interval_s: float = 15.0
    """How often 0x6D goes out; 0x6E follows after 7/15 of it, as 7 s after on the pump."""
    taken_reads: list[int] = field(default_factory=list)
    taken_writes: list[tuple[int, int]] = field(default_factory=list)
    faults: Counter[str] = field(default_factory=Counter)
    """How many of each injected fault went out."""
    _rng: random.Random = field(default_factory=lambda: random.Random(0))
    _rates: dict[str, float] = field(default_factory=dict)
    _due: deque[tuple[float, bytes]] = field(default_factory=deque)
    _next_log_set: float = 0.0
    _info_sent: float = 0.0
    _address_at: float | None = None
    _paused_until: float = 0.0
    _task: asyncio.Task[None] | None = None

    def start(self) -> None:
        self._info_sent = time.monotonic()
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task

    def inject_faults(
        self, seed: int, *, crc: float = 0.0, garbage: float = 0.0, nak: float = 0.0
    ) -> None:
        """From now on, at these rates: telegrams with a wrong checksum, stray bytes between
        telegrams, and NAKs for valid replies (the request is then not taken)."""
        self._rng = random.Random(seed)
        self._rates = {"crc": crc, "garbage": garbage, "nak": nak}

    def pause(self, seconds: float) -> None:
        """Send nothing for a while, as during a firmware update of the gateway's host."""
        self._paused_until = time.monotonic() + seconds

    def _chance(self, fault: str) -> bool:
        if self._rng.random() < self._rates.get(fault, 0.0):
            self.faults[fault] += 1
            return True
        return False

    async def _run(self) -> None:
        while True:
            pause = self._paused_until - time.monotonic()
            if pause > 0:
                await asyncio.sleep(pause)
                continue
            if self._chance("garbage"):
                # Never 0x5C, so the stray bytes can't start a telegram.
                self.end.send(bytes(self._rng.choice((0x00, 0x13, 0xA5, 0xFF)) for _ in range(3)))
            await self._token(nibe.READ_TOKEN)
            await self._send_due()
            await self._token(nibe.WRITE_TOKEN)
            await self._send_due()
            await self._unasked()
            await asyncio.sleep(self.token_interval_s)

    async def _unasked(self) -> None:
        """What the pump sends to MODBUS40 without being asked."""
        now = time.monotonic()
        if self.log_set and now >= self._next_log_set:
            self._next_log_set = now + self.log_set_interval_s
            payload = b"".join(
                r.to_bytes(2, "little") + (self.registers.get(r, 0) & 0xFFFF).to_bytes(2, "little")
                for r in self.log_set
            )
            await self._data(telegram(nibe.MODBUS40, LOG_SET, payload))
        if now >= self._info_sent + self.info_interval_s:
            self._info_sent = now
            self._address_at = now + self.info_interval_s * 7 / 15
            info = b"\x01" + self.firmware.to_bytes(2, "big") + self.product.encode("ascii")
            await self._data(telegram(nibe.MODBUS40, PRODUCT_INFO, info))
        if self._address_at is not None and now >= self._address_at:
            self._address_at = None
            await self._data(telegram(nibe.MODBUS40, MODBUS_ADDRESS, b"\x01"))

    def _send(self, frame: bytes) -> None:
        if self._chance("crc"):
            frame = frame[:-1] + bytes((frame[-1] ^ 0x5A,))
        self.end.send(frame)

    async def _token(self, command: int) -> None:
        self._send(telegram(nibe.MODBUS40, command, b""))
        first = await self.end.read_byte(self.reply_wait_s)
        if first != nibe.START_REPLY:
            return  # an ACK (nothing queued), a NAK for a damaged token, or nobody answered
        head = await self.end.expect(2)
        rest = await self.end.expect(head[1] + 1)
        frame = bytes((first,)) + head + rest
        try:
            nibe.validate_reply(frame)
        except nibe.FrameError:
            self.end.send(bytes((nibe.NAK,)))
            return
        if self._chance("nak"):
            self.end.send(bytes((nibe.NAK,)))
            return
        self.end.send(bytes((nibe.ACK,)))
        now = time.monotonic()
        register = int.from_bytes(frame[3:5], "little")
        if command == nibe.READ_TOKEN and frame[1] == nibe.READ_TOKEN:
            self.taken_reads.append(register)
            if self.silent_reads:
                self.silent_reads -= 1
            elif register not in self.absent:
                self._due.append((now + self.answer_delay_s, self._read_answer(register)))
        elif command == nibe.WRITE_TOKEN and frame[1] == nibe.WRITE_TOKEN:
            value = int.from_bytes(frame[5:9], "little")
            self.taken_writes.append((register, value))
            accepted = register not in self.refuse
            if accepted and register not in self.not_kept:
                self._apply(register, value)
            self._due.append((now + self.write_answer_delay_s, write_answer(int(accepted))))

    def _read_answer(self, register: int) -> bytes:
        if register not in self.registers32:
            return read_answer(register, dict(self.registers))
        value = self.registers32[register] & 0xFFFF_FFFF
        high, low = value >> 16, value & 0xFFFF
        first, second = (high, low) if self.registers.get(WORD_SWAP, 1) == 0 else (low, high)
        if register in self.one_word_twice:
            second = first
        return answer_with_words(register, first, second)

    def _apply(self, register: int, value: int) -> None:
        if register in self.registers32:
            self.registers32[register] = value
        elif register in self.clamp:
            lo, hi = self.clamp[register]
            self.registers[register] = min(max(_signed16(value & 0xFFFF), lo), hi) & 0xFFFF
        else:
            self.registers[register] = value

    async def _send_due(self) -> None:
        # Answers go out in time order; the two delays can reorder them.
        while True:
            due = sorted(self._due, key=lambda d: d[0])
            if not due or due[0][0] > time.monotonic():
                return
            self._due.remove(due[0])
            await self._data(due[0][1])

    async def _data(self, data: bytes) -> None:
        self._send(data)
        await self.end.read_byte(self.reply_wait_s)  # the gateway's ACK or NAK


class _Ignore(asyncio.DatagramProtocol):
    pass


class OtherClient:
    """Another program on the gateway's plain ports, as NibePi is beside Thermaestro. It
    sends requests and doesn't wait for answers; the gateway forwards it every exchange."""

    def __init__(self, host: str, read_port: int, write_port: int) -> None:
        self.host, self.read_port, self.write_port = host, read_port, write_port
        self._transport: asyncio.DatagramTransport | None = None

    async def start(self) -> None:
        loop = asyncio.get_running_loop()
        self._transport, _ = await loop.create_datagram_endpoint(
            _Ignore, local_addr=("127.0.0.1", 0)
        )

    def read(self, register: int) -> None:
        self._sendto(nibe.read_request(register), self.read_port)

    def write(self, register: int, value: int) -> None:
        self._sendto(nibe.write_request(register, value), self.write_port)

    def _sendto(self, frame: bytes, port: int) -> None:
        if self._transport is None:
            raise RuntimeError("the other client isn't started")
        self._transport.sendto(frame, (self.host, port))

    def close(self) -> None:
        if self._transport is not None:
            self._transport.close()
