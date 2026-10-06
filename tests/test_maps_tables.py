"""Model-table checks: every register of every model decodes its limits and default as the
pump would send them, and a writable one encodes them back to the same word."""

from collections.abc import Iterator

import pytest

from thermaestro.nibe.maps import EncodeError, Register, Size, Status, decode, encode, load
from thermaestro.nibe.maps.codec import NOT_CONNECTED


def registers() -> Iterator[tuple[str, str, Register]]:
    for space in ("bus", "s-series"):
        table = load(space)
        for name, model in table.models.items():
            for register in model:
                yield space, name, model.register(register)


def words_of(register: Register, raw: int) -> tuple[int, int]:
    """The words a pump sends for `raw`, high word first for 32 bits."""
    value = raw & ((1 << (register.size.bits if register.size else 16)) - 1)
    if register.size is not None and register.size.bits == 32:
        return value >> 16, value & 0xFFFF
    return value, 0


def limits(register: Register) -> list[int]:
    return [v for v in (register.min, register.max, register.default) if v is not None]


def test_every_register_decodes_its_limits_and_default() -> None:
    odd = []
    for space, model, r in registers():
        if r.size is None:
            continue
        for raw in limits(r):
            got = decode(r, *words_of(r, raw), high_word_first=True)
            if r.size is Size.S16 and raw == NOT_CONNECTED:
                assert got.status is Status.NOT_CONNECTED
            elif got.raw != raw:
                odd.append((space, model, r.id, raw, got.raw))
    assert odd == [], f"{len(odd)} limits don't fit their register's size: {odd[:10]}"


# Defaults the S-series exports give outside the register's own range: 40947 "BlockFreq 1
# stop" (25 to 118, default 120) and 45352 "Minimum permitted speed (EB101 GP12)" (1 to 50,
# default 0). Which is wrong isn't known, so the range stands and the default isn't written.
DEFAULT_OUTSIDE_RANGE = {("s-series", 40947), ("s-series", 45352)}


def test_every_writable_register_encodes_its_limits_and_default_back() -> None:
    odd = set()
    for space, model, r in registers():
        if not r.writable or r.size is None or r.size.bits == 32:
            continue
        for raw in limits(r):
            if r.size is Size.S16 and raw == NOT_CONNECTED:
                continue
            value = decode(r, *words_of(r, raw), high_word_first=True).value
            assert value is not None
            try:
                sent = encode(r, value)
            except EncodeError:
                odd.add((space, r.id))
                continue
            mask = (1 << r.size.bits) - 1
            assert sent & mask == raw & mask, (space, model, r.id, raw)
    assert odd == DEFAULT_OUTSIDE_RANGE


@pytest.mark.parametrize("space", ["bus", "s-series"])
def test_every_model_has_registers(space: str) -> None:
    for name, model in load(space).models.items():
        assert len(model.ids) > 50, name
