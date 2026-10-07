"""The price stack: what one more kWh costs, slot by slot, from the layers set up.

Each layer is a series a plugin offers (a spot price, a supplier's price) or a fixed
amount (an energy tax, a grid transfer fee), with the role it plays and whether VAT is
in it. The stack is refused, rather than computed wrong, when:
- a role is counted twice: a layer's own role, or one its series says it already covers,
  appears in another layer too (an energy tax taken from a supplier's total *and* added);
- VAT would be charged on a layer that already includes it;
- the layers are in different units.

A missing role is allowed, with a warning: a flat layer left out changes how much a kWh
costs, not when it is cheapest.

Slots are 15 minutes, from the local midnight to the next one, so a day that changes to
or from summer time has 92 or 100 of them. A series layer's value for a slot is that of
its interval holding the slot's start; a coarser interval applies to every slot it spans,
and a finer one isn't averaged.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from ..cap.model import Interval
from ..store import PriceLayer, Vat
from .series import Key, Series

SLOT = timedelta(minutes=15)
EXPECTED_ROLES = ("energy.spot", "tax.energy", "grid.transfer")


@dataclass
class Part:
    layer: str
    role: str
    value: float | None
    """As charged, VAT added where it applies."""
    vat_added: float = 0.0


@dataclass
class Slot:
    start: datetime
    end: datetime
    total: float | None
    parts: list[Part]
    missing: list[str] = field(default_factory=list)
    """Layers with no value for this slot."""


@dataclass
class Stack:
    unit: str | None
    slots: list[Slot]
    problems: list[str]
    """Why the stack is refused; no totals while there are any."""
    warnings: list[str]


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
            followed = series.followed.get(Key(layer.plugin or "", layer.series or ""))
            if followed is None:
                problems.append(f"layer {id}: {layer.plugin} offers no series {layer.series!r}")
                continue
            roles += list(followed.info.covers.value or ())
            unit = followed.info.unit
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
    held: dict[str, list[Interval]] = {}
    for id, layer in layers.items():
        if layer.source == "series":
            held[id] = await series.get(Key(layer.plugin or "", layer.series or ""), first, last)
    out = []
    for start, end in day_slots:
        parts, missing = [], []
        for id, layer in sorted(layers.items()):
            raw = layer.value if layer.source == "fixed" else _at(held[id], start)
            if raw is None:
                missing.append(id)
                parts.append(Part(id, layer.role, None))
                continue
            added = raw * vat.rate if vat is not None and _charged(vat, id, layer) else 0.0
            parts.append(Part(id, layer.role, raw + added, added))
        total = None if missing else sum(p.value or 0.0 for p in parts)
        out.append(Slot(start, end, total, parts, missing))
    return Stack(unit, out, problems, warnings)


def _at(intervals: list[Interval], moment: datetime) -> float | None:
    for interval in intervals:
        if interval.start <= moment < interval.end:
            return interval.value
    return None
