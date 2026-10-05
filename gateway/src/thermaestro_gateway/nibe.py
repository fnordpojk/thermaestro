"""Nibe MODBUS40 bus framing: the pump's telegrams and an accessory's replies.

Written from the bus description in docs/gateway-protocol.md §2. Nothing is taken from openHAB's
NibeGW or from esphome-nibe's EPL-licensed state machine.

A telegram from the pump is `5C ADDR_HI ADDR_LO CMD LEN DATA CHK`. A reply is
`C0 CMD LEN DATA CHK`. LEN counts the data bytes as sent, and the checksum is the XOR of
everything after the start byte (for a reply, including the `C0`), sent as 0xC5 when it
comes out as 0x5C. A 0x5C inside a telegram's data is sent doubled.
"""

from dataclasses import dataclass

START_TELEGRAM = 0x5C
START_REPLY = 0xC0
ACK = 0x06
NAK = 0x15
CHECKSUM_STANDING_IN_FOR_START = 0xC5

MODBUS40 = 0x0020
READ_TOKEN = 0x69
READ_ANSWER = 0x6A
WRITE_TOKEN = 0x6B
WRITE_ANSWER = 0x6C

TELEGRAM_OVERHEAD = 6  # 5C, two address bytes, CMD, LEN, CHK
REPLY_OVERHEAD = 4  # C0, CMD, LEN, CHK


class FrameError(ValueError):
    """Bytes that aren't a valid telegram, reply or exchange."""


def checksum(data: bytes) -> int:
    x = 0
    for b in data:
        x ^= b
    return CHECKSUM_STANDING_IN_FOR_START if x == START_TELEGRAM else x


def reply_frame(command: int, data: bytes) -> bytes:
    """`C0 CMD LEN DATA CHK`: an accessory's reply, e.g. a configured constant."""
    head = bytes((START_REPLY, command, len(data))) + data
    return head + bytes((checksum(head),))


def read_request(register: int) -> bytes:
    _check_register(register)
    return reply_frame(READ_TOKEN, register.to_bytes(2, "little"))


def write_request(register: int, value: int) -> bytes:
    """`value` is the raw register content as four bytes; a 16-bit register uses the low half."""
    _check_register(register)
    if not 0 <= value <= 0xFFFF_FFFF:
        raise ValueError(f"value {value} is out of range for a register write")
    return reply_frame(WRITE_TOKEN, register.to_bytes(2, "little") + value.to_bytes(4, "little"))


def validate_reply(frame: bytes) -> None:
    """Raise FrameError unless `frame` is exactly one valid reply."""
    if len(frame) < REPLY_OVERHEAD:
        raise FrameError(f"{len(frame)} bytes is shorter than any reply")
    if frame[0] != START_REPLY:
        raise FrameError(f"a reply starts with 0xc0, not {frame[0]:#04x}")
    if len(frame) != REPLY_OVERHEAD + frame[2]:
        raise FrameError(f"LEN is {frame[2]}, but {len(frame) - REPLY_OVERHEAD} data bytes follow")
    if frame[-1] != checksum(frame[:-1]):
        raise FrameError("bad checksum")


@dataclass(frozen=True, slots=True)
class Telegram:
    address: int
    command: int
    data: bytes
    """The data as sent, a doubled 0x5C still doubled."""
    payload: bytes
    """The data with each doubled 0x5C taken as one."""

    @property
    def is_token(self) -> bool:
        return not self.data


def telegram_length(data: bytes) -> int | None:
    """The length of the telegram `data` starts with, once its header has arrived."""
    if len(data) < TELEGRAM_OVERHEAD - 1:
        return None
    return TELEGRAM_OVERHEAD + data[4]


def parse_telegram(data: bytes) -> Telegram:
    """Parse exactly one telegram, or raise FrameError."""
    if not data or data[0] != START_TELEGRAM:
        raise FrameError("a telegram starts with 0x5c")
    length = telegram_length(data)
    if length is None or len(data) != length:
        raise FrameError(f"{len(data)} bytes doesn't match the telegram's LEN")
    if data[-1] != checksum(data[1:-1]):
        raise FrameError("bad checksum")
    raw = data[5:-1]
    return Telegram(
        address=int.from_bytes(data[1:3], "big"),
        command=data[3],
        data=raw,
        payload=_unescape(raw),
    )


@dataclass(frozen=True, slots=True)
class Exchange:
    """One completed bus exchange, as a NibeGW gateway forwards it in a datagram."""

    telegram: bytes
    reply: bytes
    """A `C0` reply, the gateway's or another device's; empty if there was none."""
    trailer: bytes
    """The single byte that followed (ACK, NAK); empty if none was seen."""


def split_exchange(datagram: bytes) -> Exchange:
    """Split a forwarded exchange into telegram, reply and trailing byte."""
    if not datagram or datagram[0] != START_TELEGRAM:
        raise FrameError("an exchange starts with a telegram")
    length = telegram_length(datagram)
    if length is None or len(datagram) < length:
        raise FrameError("the exchange is shorter than its telegram")
    rest = datagram[length:]
    reply = b""
    if len(rest) >= REPLY_OVERHEAD and rest[0] == START_REPLY:
        reply = rest[: REPLY_OVERHEAD + rest[2]]
        rest = rest[len(reply) :]
    if len(rest) > 1:
        raise FrameError(f"{len(rest)} unexpected bytes after the exchange")
    return Exchange(telegram=datagram[:length], reply=reply, trailer=rest)


def _check_register(register: int) -> None:
    if not 0 <= register <= 0xFFFF:
        raise ValueError(f"register {register} is out of range")


def _unescape(data: bytes) -> bytes:
    out = bytearray()
    i = 0
    while i < len(data):
        out.append(data[i])
        doubled = data[i] == START_TELEGRAM and data[i + 1 : i + 2] == bytes((START_TELEGRAM,))
        i += 2 if doubled else 1
    return bytes(out)
