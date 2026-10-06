"""LOG.SET: the list of up to 20 registers a bus-family pump pushes by itself.

The pump reads it from a USB stick (menu 7.2) and then sends those registers twice a
second, so they needn't be polled one per second each. The file is what NIBE
ModbusManager saves: Latin-1, CRLF, tab-separated:

    [NIBL;<date>;<database version>]
    Divisors<TAB><TAB><factor><TAB>…
    Date<TAB>Time<TAB><title [unit]><TAB>…
    <register>
    …

with the last register line unterminated, as ModbusManager writes it.
"""

from collections.abc import Sequence
from datetime import date

from .maps import ModelMap

MAX_REGISTERS = 20
DATABASE = 9696
"""The Nibe database version the register maps were built from."""


def render(model: ModelMap, registers: Sequence[int], *, day: date) -> bytes:
    if not 0 < len(registers) <= MAX_REGISTERS:
        raise ValueError(f"a LOG.SET holds 1 to {MAX_REGISTERS} registers, not {len(registers)}")
    defs = [model.register(r) for r in registers]
    titles = [f"{d.title} [{d.unit}]" if d.unit else d.title for d in defs]
    lines = [
        f"[NIBL;{day:%Y%m%d};{DATABASE}]",
        "\t".join(["Divisors", "", *(str(d.factor) for d in defs)]),
        "\t".join(["Date", "Time", *titles]),
        *(str(d.id) for d in defs),
    ]
    return "\r\n".join(lines).encode("latin-1")
