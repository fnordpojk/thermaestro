"""Turning a register's words into a value, and a value into what a write sends."""

from dataclasses import dataclass
from enum import Enum

from thermaestro.nibe.maps.model import Register, Size

NOT_CONNECTED = -0x8000
"""What the pump answers for a 16-bit sensor that isn't connected."""


class Status(Enum):
    OK = "ok"
    NOT_CONNECTED = "not_connected"
    OUT_OF_RANGE = "out_of_range"
    """Outside the register's range: kept, but not to be trusted."""


@dataclass(frozen=True, slots=True)
class Decoded:
    value: int | float | None
    raw: int | None
    status: Status


class UnknownSize(ValueError):
    pass


class EncodeError(ValueError):
    pass


def words(data: bytes) -> tuple[int, int]:
    """The two 16-bit words of a read answer: the register's, then the next register's."""
    if len(data) != 4:
        raise ValueError(f"a read answer carries 4 bytes, not {len(data)}")
    return int.from_bytes(data[:2], "little"), int.from_bytes(data[2:], "little")


def _signed(value: int, bits: int) -> int:
    return value - (1 << bits) if value >= 1 << (bits - 1) else value


def _in_range(register: Register, raw: int) -> bool:
    return (register.min is None or raw >= register.min) and (
        register.max is None or raw <= register.max
    )


def decode(register: Register, first: int, second: int = 0, *, high_word_first: bool) -> Decoded:
    """Decode a register from its word and the next one.

    A 32-bit value spans both; which comes first is the pump's setting 48852 (0: high word
    first). An 8-bit value is the word's low byte: pumps send a negative one either as a byte
    or sign-extended to the whole word, and both read the same.
    """
    size = register.size
    if size is None:
        raise UnknownSize(f"{register.id}: no source gives its size")
    if size.bits == 8:
        raw = first & 0xFF
    elif size.bits == 16:
        raw = first & 0xFFFF
    else:
        high, low = (first, second) if high_word_first else (second, first)
        raw = (high & 0xFFFF) << 16 | (low & 0xFFFF)
    if size.signed:
        raw = _signed(raw, size.bits)
    if size is Size.S16 and raw == NOT_CONNECTED:
        return Decoded(None, raw, Status.NOT_CONNECTED)
    value = raw if register.factor == 1 else raw / register.factor
    return Decoded(value, raw, Status.OK if _in_range(register, raw) else Status.OUT_OF_RANGE)


def encode(register: Register, value: float) -> int:
    """What a write of `value` sends: the raw value, as 32 bits.

    A negative value goes out sign-extended, as NibePi has always written negative curve
    offsets to the pump.
    """
    size = register.size
    if not register.writable:
        raise EncodeError(f"{register.id} is read-only")
    if size is None:
        raise EncodeError(f"{register.id}: no source gives its size")
    if size.bits == 32:
        # How the pump takes a 32-bit write, and whether 48852 applies to it, isn't known.
        raise EncodeError(f"{register.id}: 32-bit writes aren't verified on the bus")
    scaled = value * register.factor
    raw = round(scaled)
    if abs(scaled - raw) > 1e-6:
        raise EncodeError(f"{register.id}: {value} isn't a step of 1/{register.factor}")
    lo, hi = (
        (-(1 << (size.bits - 1)), (1 << (size.bits - 1)) - 1)
        if size.signed
        else (0, (1 << size.bits) - 1)
    )
    if not lo <= raw <= hi:
        raise EncodeError(f"{register.id}: {value} doesn't fit {size.value}")
    if size is Size.S16 and raw == NOT_CONNECTED:
        raise EncodeError(f"{register.id}: {value} is the pump's 'not connected' value")
    if not _in_range(register, raw):
        raise EncodeError(
            f"{register.id}: {value} is outside the register's range "
            f"({register.min} to {register.max}, raw)"
        )
    return raw & 0xFFFF_FFFF
