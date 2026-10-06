import pytest

from thermaestro.nibe.maps import (
    EncodeError,
    Register,
    Size,
    Status,
    UnknownSize,
    decode,
    encode,
    words,
)


def reg(
    size: Size | None,
    factor: int = 1,
    lo: int | None = None,
    hi: int | None = None,
    writable: bool = True,
) -> Register:
    return Register(
        id=40000,
        title="t",
        info="",
        unit="",
        size=size,
        factor=factor,
        min=lo,
        max=hi,
        default=None,
        writable=writable,
        sources=("test",),
        known="documented",
    )


def test_words_splits_a_read_answer() -> None:
    assert words(bytes.fromhex("0100f355")) == (0x0001, 0x55F3)
    with pytest.raises(ValueError, match="4 bytes"):
        words(b"\x01\x02")


# 32-bit values as a pump's 0x6A carried them: 43416 compressor starts and 43420 operating
# hours, with 48852 = 0 (high word first) and = 1 (low word first).
@pytest.mark.parametrize(
    ("data", "high_word_first", "value"),
    [
        ("0100f355", True, 87_539),
        ("f3550100", False, 87_539),
        ("0000be78", True, 30_910),
        ("be780000", False, 30_910),
    ],
)
def test_32_bit_word_order(data: str, high_word_first: bool, value: int) -> None:
    got = decode(reg(Size.U32), *words(bytes.fromhex(data)), high_word_first=high_word_first)
    assert (got.value, got.status) == (value, Status.OK)


def test_signed_32_bit() -> None:
    got = decode(reg(Size.S32, factor=10), 0xFFFF, 0xFF1E, high_word_first=True)
    assert got.value == pytest.approx(-22.6)


def test_16_bit_with_a_factor() -> None:
    got = decode(reg(Size.S16, factor=10), *words(bytes.fromhex("e2ff0000")), high_word_first=True)
    assert got.value == pytest.approx(-3.0)
    assert (got.raw, got.status) == (-30, Status.OK)


def test_0x8000_is_not_connected_for_s16_only() -> None:
    gone = decode(reg(Size.S16, factor=10), 0x8000, 0, high_word_first=True)
    assert (gone.value, gone.status) == (None, Status.NOT_CONNECTED)
    assert decode(reg(Size.U16), 0x8000, 0, high_word_first=True).value == 0x8000


@pytest.mark.parametrize("word", [0x00FC, 0xFFFC])
def test_s8_as_a_plain_or_sign_extended_word(word: int) -> None:
    assert decode(reg(Size.S8), word, 0, high_word_first=True).value == -4


def test_u8_reads_the_low_byte() -> None:
    assert decode(reg(Size.U8), 0x00C8, 0x1234, high_word_first=True).value == 200


def test_a_value_outside_the_registers_range_is_flagged_not_dropped() -> None:
    got = decode(reg(Size.S16, lo=0, hi=100), 150, 0, high_word_first=True)
    assert (got.value, got.status) == (150, Status.OUT_OF_RANGE)


def test_a_register_with_no_known_size_never_decodes() -> None:
    with pytest.raises(UnknownSize):
        decode(reg(None), 1, 2, high_word_first=True)


def test_encode_with_a_factor() -> None:
    assert encode(reg(Size.S16, factor=10), 25.0) == 250


def test_encode_negative_as_32_bit_twos_complement() -> None:
    # As NibePi has written negative curve offsets to the pump.
    assert encode(reg(Size.S8), -4) == 0xFFFF_FFFC
    assert encode(reg(Size.S16, factor=10), -2.5) == 0xFFFF_FFE7


@pytest.mark.parametrize(
    ("size", "values"),
    [
        (Size.S8, (-128, 0, 127)),
        (Size.U8, (0, 255)),
        (Size.S16, (-32_767, 32_767)),
        (Size.U16, (0, 65_535)),
    ],
)
def test_encode_then_decode_round_trips(size: Size, values: tuple[int, ...]) -> None:
    r = reg(size)
    for v in values:
        raw = encode(r, v)
        assert decode(r, raw & 0xFFFF, 0, high_word_first=True).value == v


@pytest.mark.parametrize(
    ("register", "value", "why"),
    [
        (reg(Size.S16, factor=10), 25.05, "step"),
        (reg(Size.S16, factor=10, lo=50, hi=700), 75.0, "range"),
        (reg(Size.U8), 256, "u8"),
        (reg(Size.S8), -129, "s8"),
        (reg(Size.S16), -32_768, "not connected"),
        (reg(Size.U8, writable=False), 1, "read-only"),
        (reg(Size.S32), 1, "32-bit"),
        (reg(None), 1, "size"),
    ],
)
def test_encode_refuses(register: Register, value: float, why: str) -> None:
    with pytest.raises(EncodeError, match=why):
        encode(register, value)
