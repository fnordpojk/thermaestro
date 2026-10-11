"""The review of a Nibe pump's own settings once NibePi has stopped: the registers NibePi is
known to change, what it did with each, and what can be done about it. And the full
comparison: every writable register the pump has, set beside its factory default.

Pure: the values come from the pump through the plugin, the defaults from the register
map. Nothing here writes; a change is a person's, through the executor.
"""

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Literal

from ..nibe import profile
from ..nibe.maps import ModelMap, Register
from ..nibe.plugin import is_switch, value_texts

Kind = Literal["nibepi", "yours", "shown", "found"]
"""NibePi changed it; you may have, through NibePi's dashboard; shown, never changed; found
by the full comparison."""


@dataclass(frozen=True)
class Entry:
    registers: tuple[int, ...]
    did: str
    """What NibePi did with it, in words."""
    kind: Kind
    recommended: bool = False
    """Putting back the value before NibePi is recommended."""
    only_if: Callable[[int | None], bool] | None = None
    """Listed only when the raw value read passes."""


def mark(text: str) -> str:
    """Marks a message for the web UI's catalog: shown translated, kept in English here."""
    return text


def _not_off(raw: int | None) -> bool:
    return raw is not None and raw != 0


ENTRIES: tuple[Entry, ...] = (
    Entry(
        tuple(s.offset for s in profile.SYSTEMS[:4] if s.offset is not None),
        mark(
            "NibePi wrote its offset here, the sum of its features' steps; when price control"
            " was switched off, the last price level's offset stayed."
        ),
        "nibepi",
    ),
    Entry(
        (47041,),
        mark(
            "NibePi set it per price level and left it at the last level's mode; a Smart"
            " Control setting was replaced."
        ),
        "nibepi",
    ),
    Entry(
        (47134,),
        mark(
            "NibePi's hot-water learning held it at 0, and didn't put it back when learning"
            " was switched off or NibePi removed: hot water then waits behind heating."
        ),
        "nibepi",
        recommended=True,
    ),
    Entry(
        (48132,),
        mark("NibePi's hot-water features set a one-time increase here; it ends by itself."),
        "nibepi",
        only_if=_not_off,
    ),
    Entry(
        tuple(range(48659, 48665)),
        mark("NibePi's frequency control rewrote these, and may have left them active."),
        "nibepi",
    ),
    Entry((49202,), mark("Left as NibePi last wrote it."), "nibepi"),
    Entry((48090, 48092), mark("Left at the last price level's values."), "nibepi"),
    Entry((47265,), mark("Left wherever NibePi's airflow control last stepped it."), "nibepi"),
    Entry(
        (47007, 47006, 47398, 47397, 47402, 47401, 47375, 47394, 47393, 47365, 47366, 47367),
        mark(
            "Set from NibePi's dashboard when one of its controls was moved: most likely your"
            " own choice. Choosing Off for the room sensor there wrote 0 even if the pump's own"
            " room control was on."
        ),
        "yours",
    ),
    Entry(
        (48852,),
        mark(
            "NibePi wrote 0 here at every start. Thermaestro reads the pump correctly either"
            " way, and so does any other program on the gateway, so it is shown and not changed."
        ),
        "shown",
    ),
)


@dataclass(frozen=True)
class Seen:
    """A register as read from the pump."""

    value: float | int | str | bool | None
    raw: int | None
    why: str | None = None
    """Why there is no value, where there isn't."""


@dataclass(frozen=True)
class Row:
    register: int
    point: str
    """Its point, under the pump's unit."""
    title: str
    unit: str | None
    did: str
    kind: Kind
    now: Seen | None
    """None until read."""
    default: float | None
    default_text: str | None
    before: float | None
    """The value before NibePi, where NibePi kept it."""
    usual: float | None
    """NibePi's own setting of it (its manual curve offset), where it had one."""
    differs: bool | None
    """From the factory default; None where not known."""
    writable: bool
    low: float | None
    high: float | None
    step: float
    names: dict[int, str] | None
    """What its values mean, where the register database says."""
    lever: str | None
    """The lever over it, whose baseline it can become."""
    recommended: bool


def point(register: int) -> str:
    return f"{profile.UNIT}/x.nibe.{register}"


def listed(model: ModelMap) -> list[int]:
    """The listed registers this pump has, in the list's order."""
    return [r for e in ENTRIES for r in e.registers if r in model]


def comparable(model: ModelMap) -> list[int]:
    """Every register of the full comparison: writable, with a default, a size it can be
    read in, and not on the list."""
    skip = set(listed(model))
    out = []
    for r in sorted(model.ids):
        reg = model.register(r)
        if r in skip or not reg.writable or reg.default is None:
            continue
        if reg.size is None or reg.size.bits > 16:
            continue
        out.append(r)
    return out


def review(
    model: ModelMap,
    seen: Mapping[int, Seen],
    *,
    before: Mapping[int, float],
    usual: Mapping[int, float],
    levers: Mapping[int, str],
) -> list[Row]:
    """The listed registers this pump has. `usual`: per register, NibePi's own setting of
    it; `levers`: per register, the lever over it."""
    rows = []
    for entry in ENTRIES:
        for register in entry.registers:
            if register not in model:
                continue
            now = seen.get(register)
            if entry.only_if is not None and now is not None and not entry.only_if(now.raw):
                continue
            rows.append(
                _row(
                    model.register(register),
                    entry,
                    now,
                    before.get(register),
                    usual.get(register),
                    levers.get(register),
                )
            )
    return rows


def compared(model: ModelMap, seen: Mapping[int, Seen], levers: Mapping[int, str]) -> list[Row]:
    """The registers of the full comparison read so far that differ from their default."""
    found = Entry((), mark("Differs from the factory default."), "found")
    out = []
    for register in comparable(model):
        now = seen.get(register)
        if now is None:
            continue
        row = _row(model.register(register), found, now, None, None, levers.get(register))
        if row.differs:
            out.append(row)
    return out


def _row(
    reg: Register,
    entry: Entry,
    now: Seen | None,
    before: float | None,
    usual: float | None,
    lever: str | None,
) -> Row:
    names = None if is_switch(reg) else (reg.mappings or value_texts(reg))
    default = None if reg.default is None else reg.default / reg.factor
    default_text = None
    if reg.default is not None:
        if is_switch(reg):
            default_text = "on" if reg.default else "off"
        elif names and reg.default in names:
            default_text = names[reg.default]
        else:
            default_text = f"{default:g}"
    differs = None
    if now is not None and now.raw is not None and reg.default is not None:
        differs = now.raw != reg.default
    return Row(
        register=reg.id,
        point=point(reg.id),
        title=reg.title,
        unit=profile.unit(reg.unit),
        did=entry.did,
        kind=entry.kind,
        now=now,
        default=default,
        default_text=default_text,
        before=before,
        usual=usual,
        differs=differs,
        writable=reg.writable and entry.kind != "shown",
        low=None if reg.min is None else reg.min / reg.factor,
        high=None if reg.max is None else reg.max / reg.factor,
        step=1 / reg.factor,
        names=dict(names) if names else None,
        lever=lever,
        recommended=entry.recommended and before is not None,
    )


def offsets_by_register(offsets: Mapping[int, float]) -> dict[int, float]:
    """NibePi's manual curve offset per climate system, by the register it is the offset of."""
    out = {}
    for system in profile.SYSTEMS:
        if system.offset is not None and system.number in offsets:
            out[system.offset] = offsets[system.number]
    return out


def touched(levers: Iterable[tuple[str, Iterable[str]]]) -> dict[int, str]:
    """Per register, the lever whose `touches` name it: `(ref, touches)` pairs."""
    out: dict[int, str] = {}
    for ref, touches in levers:
        for datapoint in touches:
            if datapoint.startswith("x.nibe.") and datapoint[7:].isdigit():
                out.setdefault(int(datapoint[7:]), ref)
    return out
