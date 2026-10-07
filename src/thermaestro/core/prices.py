"""The price stack: what one more kWh costs, slot by slot, from the layers set up.

Each layer is a series a plugin offers (a spot price, a supplier's price) or a fixed
amount (an energy tax, a grid transfer fee), with the role it plays and whether VAT is
in it. The stack is refused, rather than computed wrong, when:
- a role is counted twice: a layer's own role, or one its series says it already covers,
  appears in another layer too (an energy tax taken from a supplier's total *and* added);
- VAT would be charged on a layer that already includes it;
- the layers are in different units;
- a layer's fallback isn't the same kind of price as the layer's own series.

A missing role is allowed, with a warning: a flat layer left out changes how much a kWh
costs, not when it is cheapest.

Slots are 15 minutes, from the local midnight to the next one, so a day that changes to
or from summer time has 92 or 100 of them. A series layer's value for a slot is that of
its interval holding the slot's start; a coarser interval applies to every slot it spans,
and a finer one isn't averaged. Where the layer's series has no price, its fallbacks are
tried in order.

The stack is checked against every other price series offered that covers some of its
layers exactly: a supplier's price with VAT (Tibber's total) checks the spot price plus
the supplier's adders plus VAT on both; another source's spot price checks the spot
price. A slot differs when the two are more than 1 % apart.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from ..cap.model import Interval, SeriesInfo
from ..store import PriceLayer, Vat
from .series import Key, Series

SLOT = timedelta(minutes=15)
EXPECTED_ROLES = ("energy.spot", "tax.energy", "grid.transfer")
CHECK_RELATIVE = 0.01
CHECK_ABSOLUTE = 0.001
"""A check differs where the stack and the series are further apart than 1 % of the
series' value, and than this, which covers prices published rounded."""


@dataclass
class Part:
    layer: str
    role: str
    value: float | None
    """As charged, VAT added where it applies."""
    vat_added: float = 0.0
    fallback: str | None = None
    """The series that stood in for the layer's own, `<instance>:<series>`."""


@dataclass
class Slot:
    start: datetime
    end: datetime
    total: float | None
    parts: list[Part]
    missing: list[str] = field(default_factory=list)
    """Layers with no value for this slot."""


@dataclass
class Check:
    """The stack compared with a series that covers some of its layers."""

    series: str
    """`<instance>:<series>`."""
    layers: list[str]
    """The layers it covers."""
    compared: int = 0
    differing: int = 0
    largest: float = 0.0
    """The largest difference, the series' value minus the stack's."""
    at: datetime | None = None
    """The start of the slot with the largest difference."""


@dataclass
class Stack:
    unit: str | None
    slots: list[Slot]
    problems: list[str]
    """Why the stack is refused; no totals while there are any."""
    warnings: list[str]
    checks: list[Check] = field(default_factory=list)


def _ref(instance: str, series: str) -> str:
    return f"{instance}:{series}"


def _key(ref: str) -> Key:
    instance, _, series = ref.partition(":")
    return Key(instance, series)


def _info(series: Series, ref: str) -> SeriesInfo | None:
    followed = series.followed.get(_key(ref))
    return followed.info if followed else None


def _covered(info: SeriesInfo) -> set[str]:
    return {info.role, *(info.covers.value or ())}


def check(
    layers: Mapping[str, PriceLayer], vat: Vat | None, series: Series
) -> tuple[list[str], list[str], str | None]:
    """The refusals and warnings for a set of layers, and their common unit."""
    problems: list[str] = []
    counted: dict[str, str] = {}
    units: set[str] = set()
    for id, layer in sorted(layers.items()):
        roles = [layer.role]
        unit = layer.unit
        if layer.source == "series":
            info = _info(series, _ref(layer.plugin or "", layer.series or ""))
            if info is None:
                problems.append(f"layer {id}: {layer.plugin} offers no series {layer.series!r}")
                continue
            roles += list(info.covers.value or ())
            unit = info.unit
            problems += _fallback_problems(id, info, layer, series)
        units.add(unit)
        for role in roles:
            if role in counted:
                problems.append(f"{role} is counted twice: in {counted[role]} and in {id}")
            else:
                counted[role] = id
        if vat is not None and _charged(vat, id, layer) and (layer.vat == "incl" or "vat" in roles):
            problems.append(f"VAT would be charged on {id}, which already includes it")
    if len(units) > 1:
        problems.append(f"the layers are in different units: {', '.join(sorted(units))}")
    warnings = [f"no layer for {role}" for role in EXPECTED_ROLES if role not in counted]
    if layers and vat is None and any(layer.vat == "excl" for layer in layers.values()):
        warnings.append("some layers exclude VAT, and no VAT is set")
    return problems, warnings, (units.pop() if len(units) == 1 else None)


def _fallback_problems(id: str, info: SeriesInfo, layer: PriceLayer, series: Series) -> list[str]:
    out = []
    for ref in layer.fallbacks:
        other = _info(series, ref)
        if other is None:
            out.append(f"layer {id}: no series {ref} to fall back on")
        elif _covered(other) != _covered(info):
            out.append(f"layer {id}: {ref} isn't the same price ({other.role}, not {info.role})")
        elif (other.unit, other.vat) != (info.unit, info.vat):
            out.append(
                f"layer {id}: {ref} is in {other.unit}, VAT {other.vat};"
                f" the layer in {info.unit}, VAT {info.vat}"
            )
    return out


def _charged(vat: Vat, id: str, layer: PriceLayer) -> bool:
    """Whether the VAT rule names this layer, by id or by role."""
    return id in vat.applies_to or layer.role in vat.applies_to


def slots(day: date, zone: ZoneInfo) -> list[tuple[datetime, datetime]]:
    """The day's 15-minute slots, from local midnight to local midnight."""
    start = datetime.combine(day, time(0), zone).astimezone(UTC)
    end = datetime.combine(day + timedelta(days=1), time(0), zone).astimezone(UTC)
    out = []
    t = start
    while t < end:
        out.append((t, t + SLOT))
        t += SLOT
    return out


async def assemble(
    layers: Mapping[str, PriceLayer],
    vat: Vat | None,
    series: Series,
    day: date,
    zone: ZoneInfo,
) -> Stack:
    problems, warnings, unit = check(layers, vat, series)
    day_slots = slots(day, zone)
    if problems or not layers:
        return Stack(unit, [], problems, warnings)
    first, last = day_slots[0][0].timestamp(), day_slots[-1][1].timestamp()
    held: dict[str, list[tuple[str | None, list[Interval]]]] = {}
    for id, layer in layers.items():
        if layer.source == "series":
            own = _ref(layer.plugin or "", layer.series or "")
            held[id] = [(None, await series.get(_key(own), first, last))]
            for ref in layer.fallbacks:
                held[id].append((ref, await series.get(_key(ref), first, last)))
    out = []
    for start, end in day_slots:
        parts, missing = [], []
        for id, layer in sorted(layers.items()):
            fallback: str | None = None
            if layer.source == "fixed":
                raw = layer.value
            else:
                raw = None
                for stand_in, intervals in held[id]:
                    raw = _at(intervals, start)
                    if raw is not None:
                        fallback = stand_in
                        break
            if raw is None:
                missing.append(id)
                parts.append(Part(id, layer.role, None))
                continue
            added = raw * vat.rate if vat is not None and _charged(vat, id, layer) else 0.0
            parts.append(Part(id, layer.role, raw + added, added, fallback))
        total = None if missing else sum(p.value or 0.0 for p in parts)
        out.append(Slot(start, end, total, parts, missing))
    checks = await _checks(layers, series, unit, out, first, last)
    for c in checks:
        if c.differing:
            warnings.append(
                f"{c.series} differs from the stack's {' + '.join(c.layers)} in {c.differing}"
                f" of {c.compared} quarters, by up to {abs(c.largest):.4f} {unit}"
            )
    return Stack(unit, out, problems, warnings, checks)


async def _checks(
    layers: Mapping[str, PriceLayer],
    series: Series,
    unit: str | None,
    stack: list[Slot],
    first: float,
    last: float,
) -> list[Check]:
    """Compare the stack with every other price series that covers some layers exactly."""
    used = set()
    roles: dict[str, set[str]] = {}
    for id, layer in layers.items():
        roles[id] = {layer.role}
        if layer.source == "series":
            own = _ref(layer.plugin or "", layer.series or "")
            used |= {own, *layer.fallbacks}
            info = _info(series, own)
            roles[id] |= _covered(info) if info else set()
    out = []
    for key, followed in sorted(
        series.followed.items(), key=lambda kv: (kv[0].instance, kv[0].series)
    ):
        info = followed.info
        ref = _ref(key.instance, key.series)
        if ref in used or info.kind != "price" or info.unit != unit or info.covers.value is None:
            continue
        wanted = _covered(info)
        chosen = sorted(id for id, r in roles.items() if r & wanted)
        if not chosen or set().union(*(roles[id] for id in chosen)) - {"vat"} != wanted - {"vat"}:
            continue
        with_vat = "vat" in wanted or info.vat == "incl"
        if not with_vat and any(layers[id].vat == "incl" for id in chosen):
            continue
        intervals = await series.get(key, first, last)
        c = Check(ref, chosen)
        for slot in stack:
            parts = [p for p in slot.parts if p.layer in chosen]
            theirs = _at(intervals, slot.start)
            if theirs is None or any(p.value is None for p in parts):
                continue
            ours = sum((p.value or 0.0) - (0.0 if with_vat else p.vat_added) for p in parts)
            difference = theirs - ours
            c.compared += 1
            if abs(difference) > max(CHECK_ABSOLUTE, CHECK_RELATIVE * abs(theirs)):
                c.differing += 1
            if c.at is None or abs(difference) > abs(c.largest):
                c.largest, c.at = difference, slot.start
        if c.compared:
            out.append(c)
    return out


def _at(intervals: list[Interval], moment: datetime) -> float | None:
    for interval in intervals:
        if interval.start <= moment < interval.end:
            return interval.value
    return None
