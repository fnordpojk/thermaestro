"""The price stack: what one more kWh costs, slot by slot, from the layers set up.

Each layer is a series a plugin offers (a spot price, a supplier's price), a fixed
amount (an energy tax, a grid transfer fee), or a grid rule's time-of-use prices, with the
role it plays and whether VAT is in it. The stack is refused, rather than computed wrong, when:
- a role is counted twice: a layer's own role, or one its series says it already covers,
  appears in another layer too (an energy tax taken from a supplier's total *and* added);
- VAT would be charged on a layer that already includes it;
- the layers are in different units;
- a layer's fallback isn't the same kind of price as the layer's own series;
- a rule layer's rule isn't there, isn't a time-of-use rule, or is in another unit.

A missing role is allowed, with a warning: a flat layer left out changes how much a kWh
costs, not when it is cheapest.

Slots are 15 minutes, from the local midnight to the next one, so a day that changes to
or from summer time has 92 or 100 of them. A series layer's value for a slot is that of
its interval holding the slot's start; a coarser interval applies to every slot it spans,
and a finer one isn't averaged. Where the layer's series has no price, its fallbacks are
tried in order.

A supplier's total beside a spot layer is split, where the supplier gives its own spot
price too (Tibber's total and its energy price): the total less the supplier's own spot
price, with VAT where the total has it, is what the supplier adds, and the spot price
comes from the spot layer, from any source and with its fallbacks. On a day the supplier
has no prices, the last it added applies, from up to a week back: a supplier's adders
seldom change.

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
from ..store import GridRule, PriceLayer, Vat
from .gridrules import Holiday, tou_price
from .series import Key, Series

SLOT = timedelta(minutes=15)
CARRY = timedelta(days=7)
"""How far back a split total's last adders are looked for."""
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
    carried_from: datetime | None = None
    """For a split total with no price of its own here: where its last price started."""


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


@dataclass(frozen=True)
class Split:
    """A layer holding a supplier's total, from which the supplier's spot price is taken."""

    total: str
    spot: str
    """The supplier's own spot price, `<instance>:<series>`."""
    with_vat: bool
    """Whether the total includes VAT, so the spot price is taken out with it."""


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


def splits(layers: Mapping[str, PriceLayer], series: Series) -> dict[str, Split]:
    """The layers holding a supplier's total that are split: a series that includes the
    spot price, in a stack that has a spot layer, from a supplier that offers its own spot
    price in the same unit."""
    if not any(layer.role == "energy.spot" for layer in layers.values()):
        return {}
    out = {}
    for id, layer in sorted(layers.items()):
        if layer.source != "series" or layer.role == "energy.spot":
            continue
        ref = _ref(layer.plugin or "", layer.series or "")
        info = _info(series, ref)
        if info is None or "energy.spot" not in (info.covers.value or ()):
            continue
        own = next(
            (
                _ref(key.instance, key.series)
                for key, followed in sorted(
                    series.followed.items(), key=lambda kv: (kv[0].instance, kv[0].series)
                )
                if key.instance == layer.plugin
                and followed.info.kind == "price"
                and followed.info.role == "energy.spot"
                and not followed.info.covers.value
                and (followed.info.unit, followed.info.vat) == (info.unit, "excl")
            ),
            None,
        )
        if own is not None:
            out[id] = Split(ref, own, info.vat == "incl")
    return out


def check(
    layers: Mapping[str, PriceLayer],
    vat: Vat | None,
    series: Series,
    rules: Mapping[str, GridRule] | None = None,
) -> tuple[list[str], list[str], str | None]:
    """The refusals and warnings for a set of layers, and their common unit."""
    problems: list[str] = []
    counted: dict[str, str] = {}
    units: set[str] = set()
    split = splits(layers, series)
    for id, layer in sorted(layers.items()):
        roles = [layer.role]
        unit = layer.unit
        if layer.source == "series":
            info = _info(series, _ref(layer.plugin or "", layer.series or ""))
            if info is None:
                problems.append(f"layer {id}: {layer.plugin} offers no series {layer.series!r}")
                continue
            covers = list(info.covers.value or ())
            if id in split:
                covers = [role for role in covers if role != "energy.spot"]
                if split[id].with_vat and vat is None:
                    problems.append(
                        f"layer {id}: its spot price is taken out with VAT, and no VAT is set"
                    )
            roles += covers
            unit = info.unit
            problems += _fallback_problems(id, info, layer, series)
        elif layer.source == "rule":
            problems += _rule_problems(id, layer, (rules or {}).get(layer.rule or ""))
        units.add(unit)
        for role in roles:
            if role in counted:
                problems.append(
                    f"{role} is counted twice, in the layers {counted[role]} and {id}:"
                    " remove one under Setup → Prices"
                )
            else:
                counted[role] = id
        if vat is not None and _charged(vat, id, layer) and (layer.vat == "incl" or "vat" in roles):
            problems.append(f"VAT would be charged on {id}, which already includes it")
    if len(units) > 1:
        problems.append(f"the layers are in different units: {', '.join(sorted(units))}")
    warnings = [f"no layer for {role}" for role in EXPECTED_ROLES if role not in counted]
    if layers and vat is None and any(layer.vat == "excl" for layer in layers.values()):
        warnings.append("some layers exclude VAT, and no VAT is set")
    if vat is not None:
        warnings += [
            f"VAT isn't charged on {id}, which excludes it"
            for id, layer in sorted(layers.items())
            if layer.vat == "excl" and not _charged(vat, id, layer)
        ]
    return problems, warnings, (units.pop() if len(units) == 1 else None)


def _rule_problems(id: str, layer: PriceLayer, rule: GridRule | None) -> list[str]:
    if rule is None:
        return [f"layer {id}: there is no grid rule {layer.rule!r}"]
    if rule.type != "tou":
        return [f"layer {id}: the grid rule {layer.rule} has no prices per kWh"]
    if rule.unit != layer.unit:
        return [
            f"layer {id}: the grid rule {layer.rule} is in {rule.unit}, the layer in {layer.unit}"
        ]
    return []


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


def _adders(
    totals: list[Interval], spots: list[Interval], factor: float
) -> list[tuple[datetime, datetime, float]]:
    """Each total's interval with what it holds beyond the spot price: the total less
    the spot price times `factor` (1 + VAT, where the total includes it)."""
    out = []
    for interval in totals:
        spot = _at(spots, interval.start)
        if spot is not None:
            out.append((interval.start, interval.end, interval.value - factor * spot))
    return out


def _adders_at(
    adders: list[tuple[datetime, datetime, float]], moment: datetime
) -> tuple[float, datetime | None] | None:
    """The adders at a moment, or the last before it and where those started."""
    last: tuple[datetime, datetime, float] | None = None
    for start, end, value in adders:
        if start <= moment < end:
            return value, None
        if end <= moment and (last is None or start > last[0]):
            last = (start, end, value)
    return (last[2], last[0]) if last is not None else None


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
    rules: Mapping[str, GridRule] | None = None,
    holiday: Holiday | None = None,
) -> Stack:
    """The day's stack. `rules`: the grid rules a rule layer names; `holiday`: the public
    holidays, for a rule's working days."""
    problems, warnings, unit = check(layers, vat, series, rules)
    day_slots = slots(day, zone)
    if problems or not layers:
        return Stack(unit, [], problems, warnings)
    first, last = day_slots[0][0].timestamp(), day_slots[-1][1].timestamp()
    split = splits(layers, series)
    rate = vat.rate if vat is not None else 0.0
    held: dict[str, list[tuple[str | None, list[Interval]]]] = {}
    adders: dict[str, list[tuple[datetime, datetime, float]]] = {}
    for id, layer in layers.items():
        if id in split:
            since = first - CARRY.total_seconds()
            totals = await series.get(_key(split[id].total), since, last)
            spots = await series.get(_key(split[id].spot), since, last)
            adders[id] = _adders(totals, spots, 1 + rate if split[id].with_vat else 1.0)
        elif layer.source == "series":
            own = _ref(layer.plugin or "", layer.series or "")
            held[id] = [(None, await series.get(_key(own), first, last))]
            for ref in layer.fallbacks:
                held[id].append((ref, await series.get(_key(ref), first, last)))
    out = []
    for start, end in day_slots:
        parts, missing = [], []
        for id, layer in sorted(layers.items()):
            fallback: str | None = None
            carried: datetime | None = None
            if layer.source == "fixed":
                raw = layer.value
            elif layer.source == "rule":
                raw = tou_price((rules or {})[layer.rule or ""], start, zone, holiday)
            elif id in split:
                found = _adders_at(adders[id], start)
                raw, carried = found if found is not None else (None, None)
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
            if id in split and split[id].with_vat:
                # VAT is in the value already; its share is shown as VAT.
                parts.append(Part(id, layer.role, raw, raw * rate / (1 + rate), None, carried))
                continue
            added = raw * vat.rate if vat is not None and _charged(vat, id, layer) else 0.0
            parts.append(Part(id, layer.role, raw + added, added, fallback, carried))
        total = None if missing else sum(p.value or 0.0 for p in parts)
        out.append(Slot(start, end, total, parts, missing))
    checks = await _checks(layers, split, series, unit, out, first, last)
    for c in checks:
        if c.differing:
            warnings.append(
                f"{c.series} differs from the stack's {' + '.join(c.layers)} in {c.differing}"
                f" of {c.compared} quarters, by up to {abs(c.largest):.4f} {unit}"
            )
    return Stack(unit, out, problems, warnings, checks)


async def _checks(
    layers: Mapping[str, PriceLayer],
    split: Mapping[str, Split],
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
            if id in split:
                roles[id].discard("energy.spot")
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
