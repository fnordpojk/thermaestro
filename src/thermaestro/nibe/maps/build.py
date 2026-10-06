"""Build a register table from its sources, as `scripts/make-registermaps.py` does for the
tables that ship and a user does for their own export.

The sources, and how much each is trusted:
- Nibe's ModbusManager exports, the base of the bus table: documented.
- The S-series register exports in the nibe library (made on real pumps, from the pump's own
  menu), the base of the S-series table: documented.
- NibePi's model files: older exports, plus a few registers no current export lists and
  product names the exports lack. What only they give is reported.
- The nibe library's extensions.json, hand-made corrections: reported.
- Nibe's S-series register list, names only: attached to the registers it names.

Reported knowledge never replaces a documented definition. Where a source disagrees with the
definition used, the disagreement is kept on the register.
"""

import csv
import json
import re
from collections import Counter
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field, fields
from typing import Any

FORMAT = 1
SOURCE_NAMES = {
    "nibepi": "NibePi's model files (github.com/anerdins/nibepi)",
    "nibe-lib": "Register exports from S-series pumps, collected in the nibe library "
    "(github.com/yozik04/nibe)",
    "nibe-lib-ext": "Hand-made corrections in the nibe library's extensions.json",
    "nibe-modbus-list": "Nibe's S-series Modbus register list (names only)",
}
TYPE_FIELDS = ("size", "factor", "writable")
"""What changes how a value decodes, or whether it can be written."""
VARIANT_FIELDS = (*TYPE_FIELDS, "unit", "min", "max", "default")


class BuildError(ValueError):
    pass


def documented(source: str) -> bool:
    """Vendor exports: Nibe's database, and S-series pumps' own exports."""
    return source.startswith("nibe-db-") or source == "nibe-lib"


@dataclass(frozen=True, slots=True)
class Definition:
    title: str
    info: str
    unit: str
    size: str | None
    factor: int
    min: int | None
    max: int | None
    default: int | None
    writable: bool

    def key(self, names: Sequence[str]) -> tuple[Any, ...]:
        return tuple(getattr(self, n) for n in names)


@dataclass(frozen=True, slots=True)
class Export:
    """One documented export: the products it covers and their registers."""

    name: str
    source: str
    products: tuple[str, ...]
    defs: Mapping[int, Definition]
    clashes: Mapping[int, tuple[Definition, ...]] = field(default_factory=dict)
    """Entries the export lists on a register's number besides the one used."""


@dataclass(frozen=True, slots=True)
class SourceModel(Mapping[int, Definition]):
    """One of NibePi's model files: a product and its registers."""

    product: str
    defs: Mapping[int, Definition]

    def __getitem__(self, register: int) -> Definition:
        return self.defs[register]

    def __iter__(self) -> Iterator[int]:
        return iter(self.defs)

    def __len__(self) -> int:
        return len(self.defs)


@dataclass(frozen=True, slots=True)
class ExtBlock:
    products: tuple[str, ...]
    data: Mapping[int, Mapping[str, Any]]


def _int(text: object) -> int | None:
    if text is None:
        return None
    s = str(text).strip()
    if s in ("", "-"):
        return None
    return int(float(s))


def _limits(lo: object, hi: object) -> tuple[int | None, int | None]:
    a, b = _int(lo), _int(hi)
    if (a or 0) == 0 and (b or 0) == 0:
        return None, None  # Nibe's 0/0: no range given
    return a, b


def _size(text: object) -> str | None:
    s = str(text or "").strip().lower()
    return s if s in ("s8", "u8", "s16", "u16", "s32", "u32") else None


def _add(defs: dict[int, Definition], register: int, d: Definition, where: str) -> None:
    old = defs.get(register)
    if old is not None and old.key(TYPE_FIELDS) != d.key(TYPE_FIELDS):
        raise BuildError(f"{where}: register {register} is defined twice, differently")
    defs.setdefault(register, d)


def products_of(name: str) -> tuple[str, ...]:
    """The nibe library names its files after their products: "vvms320_vvms325.csv"."""
    stem = name.rsplit("/", 1)[-1].removesuffix(".json").removesuffix(".csv")
    return tuple(p.upper() for p in stem.split("_"))


def read_modbusmanager(data: bytes, name: str) -> Export:
    """A ModbusManager CSV: Latin-1, `;`-separated, four header lines naming the products and
    the database version."""
    lines = data.decode("latin-1").splitlines()
    header = {k.strip(): v.strip() for k, _, v in (ln.partition(":") for ln in lines[:4]) if v}
    if "Product" not in header or "Database" not in header:
        raise BuildError(f"{name}: not a ModbusManager export")
    products = tuple(p.strip() for p in header["Product"].split(","))
    defs: dict[int, Definition] = {}
    for row in csv.reader(lines[4:], delimiter=";"):
        if len(row) < 10 or not row[2].strip().isdigit():
            continue
        lo, hi = _limits(row[6], row[7])
        d = Definition(
            row[0],
            row[1],
            row[3],
            _size(row[4]),
            _int(row[5]) or 1,
            lo,
            hi,
            _int(row[8]),
            row[9].strip().upper() == "R/W",
        )
        _add(defs, int(row[2]), d, name)
    return Export(name, f"nibe-db-{header['Database']}", products, defs)


def read_s_export(data: bytes, name: str) -> Export:
    """An S-series pump's own register export, as the nibe library keeps it: tab-separated,
    with the register type and number.

    Such an export also lists internal parameters as "id:NNNN", some on a number a named
    register has. On a shared number the named entry is the register; where entries still
    differ in type, the one that gives a size is used and the others are kept as clashes.
    """
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        text = data.decode("latin-1")
    entries: dict[int, list[Definition]] = {}
    for row in csv.DictReader(text.splitlines(), delimiter="\t"):
        kind, number = row.get("Register type") or "", (row.get("Register") or "").strip()
        if not number.isdigit():
            continue
        if "INPUT" in kind:
            register, writable = 30_000 + int(number), False
        elif "HOLDING" in kind:
            register, writable = 40_000 + int(number), True
        else:
            continue
        lo, hi = _limits(row.get("Min value"), row.get("Max value"))
        d = Definition(
            row["Title"],
            "",
            row.get("Unit") or "",
            _size(row.get("Size of variable")),
            _int(row.get("Division factor")) or 1,
            lo,
            hi,
            _int(row.get("Default value")),
            writable,
        )
        entries.setdefault(register, []).append(d)
    defs: dict[int, Definition] = {}
    clashes: dict[int, tuple[Definition, ...]] = {}
    for register, ds in entries.items():
        named = [d for d in ds if not d.title.startswith("id:")] or ds
        used = next((d for d in named if d.size is not None), named[0])
        defs[register] = used
        other = tuple(d for d in named if d.key(TYPE_FIELDS) != used.key(TYPE_FIELDS))
        if other:
            clashes[register] = other
    return Export(name, "nibe-lib", products_of(name), defs, clashes)


def read_nibepi(data: bytes, product: str) -> SourceModel:
    """One of NibePi's model files, `models/<product>.json`."""
    defs: dict[int, Definition] = {}
    for e in json.loads(data.decode("utf-8", errors="replace")):
        lo, hi = _limits(e.get("min"), e.get("max"))
        d = Definition(
            e.get("titel", ""),
            e.get("info", ""),
            e.get("unit", ""),
            _size(e.get("size")),
            _int(e.get("factor")) or 1,
            lo,
            hi,
            None,
            str(e.get("mode", "")).upper() == "R/W",
        )
        _add(defs, int(e["register"]), d, f"NibePi {product}")
    return SourceModel(product, defs)


def read_extensions(data: bytes) -> list[ExtBlock]:
    blocks = []
    for b in json.loads(data):
        products = tuple(dict.fromkeys(p for f in b["files"] for p in products_of(f)))
        blocks.append(ExtBlock(products, {int(k): v for k, v in b["data"].items()}))
    return blocks


def read_official_list(text: str) -> dict[int, str]:
    """Names from Nibe's S-series register list, as `pdftotext -layout` gives it."""
    names = {}
    for m in re.finditer(r"eMb(Input|Holding)_(\w+)\s*=\s*(\d+)", text):
        number = int(m.group(3))
        if number < 10_000:
            names[(30_000 if m.group(1) == "Input" else 40_000) + number] = m.group(2)
    return names


@dataclass
class _Table:
    """The registers per product while a table is built."""

    defs: dict[str, dict[int, Definition]] = field(default_factory=dict)
    sources: dict[int, dict[str, None]] = field(default_factory=dict)
    disagreements: dict[int, dict[tuple[str, str, str], None]] = field(default_factory=dict)
    mappings: dict[int, dict[int, str]] = field(default_factory=dict)

    def source(self, register: int, source: str) -> None:
        self.sources.setdefault(register, {})[source] = None

    def disagree(self, register: int, source: str, name: str, value: object) -> None:
        self.disagreements.setdefault(register, {})[(source, name, str(value))] = None

    def compare(self, register: int, used: Definition, other: Definition, source: str) -> None:
        for name in TYPE_FIELDS:
            if getattr(other, name) != getattr(used, name):
                self.disagree(register, source, name, getattr(other, name))


def _documented_base(table: _Table) -> dict[int, Definition]:
    """Per register, the definition most documented products share."""
    seen: dict[int, list[Definition]] = {}
    for product in sorted(table.defs):
        for register, d in table.defs[product].items():
            seen.setdefault(register, []).append(d)
    return {r: _most_common(ds) for r, ds in seen.items()}


def _most_common(defs: Sequence[Definition]) -> Definition:
    counts = Counter(d.key(VARIANT_FIELDS) for d in defs)
    best = max(counts.values())
    return next(d for d in defs if counts[d.key(VARIANT_FIELDS)] == best)


def _apply_nibepi(table: _Table, models: Iterable[SourceModel]) -> None:
    base = _documented_base(table)
    added: dict[int, Definition] = {}
    for model in models:
        known = table.defs.setdefault(model.product, {})
        for register, d in model.defs.items():
            used = known.get(register) or base.get(register) or added.get(register)
            if used is None:
                added[register] = used = d
            else:
                table.compare(register, used, d, "nibepi")
            known.setdefault(register, used)
            table.source(register, "nibepi")


def _apply_extensions(table: _Table, blocks: Iterable[ExtBlock]) -> None:
    for block in blocks:
        for product in block.products:
            defs = table.defs.get(product)
            if defs is None:
                continue
            for register, given in block.data.items():
                if register not in defs and given.get("size"):
                    lo, hi = _int(given.get("min")), _int(given.get("max"))
                    defs[register] = Definition(
                        given.get("title", ""),
                        given.get("info", ""),
                        given.get("unit") or "",
                        _size(given["size"]),
                        _int(given.get("factor")) or 1,
                        lo,
                        hi,
                        _int(given.get("default")),
                        bool(given.get("write", False)),
                    )
                    table.source(register, "nibe-lib-ext")
                    continue
                if register not in defs:
                    continue
                used = defs[register]
                for key, value in given.items():
                    if key == "mappings":
                        if isinstance(value, dict):
                            names = {int(k): v for k, v in value.items() if v is not None}
                            old = table.mappings.get(register)
                            if old is not None and old != names:
                                table.disagree(register, "nibe-lib-ext", "mappings", names)
                            else:
                                table.mappings[register] = names
                        continue
                    name = {"write": "writable"}.get(key, key)
                    if name not in VARIANT_FIELDS:
                        continue
                    have = getattr(used, name)
                    norm = (
                        _size(value)
                        if name == "size"
                        else bool(value)
                        if name == "writable"
                        else (value or "")
                        if name == "unit"
                        else _int(value)
                    )
                    if norm != have:
                        table.disagree(register, "nibe-lib-ext", name, value)
                table.source(register, "nibe-lib-ext")


def _title(defs: Iterable[Definition]) -> Definition:
    """A definition whose title names the register; S-series exports list some as `id:…`."""
    ds = list(defs)
    return next((d for d in ds if not d.title.startswith("id:")), ds[0])


def build(
    space: str,
    *,
    modbusmanager: Sequence[Export] = (),
    s_exports: Sequence[Export] = (),
    nibepi: Sequence[SourceModel] = (),
    extensions: Sequence[ExtBlock] = (),
    official: Mapping[int, str] | None = None,
) -> dict[str, Any]:
    """The table for one register space, as the JSON that ships."""
    if space not in ("bus", "s-series"):
        raise BuildError(f"unknown register space {space!r}")
    table = _Table()
    names: dict[str, str] = {}
    for export in (*modbusmanager, *s_exports):
        names[export.source] = (
            f"NIBE ModbusManager export, database {export.source.removeprefix('nibe-db-')}"
            if export.source.startswith("nibe-db-")
            else SOURCE_NAMES[export.source]
        )
        for product in export.products:
            defs = table.defs.setdefault(product, {})
            for register, d in export.defs.items():
                if register in defs:
                    table.compare(register, defs[register], d, export.source)
                else:
                    defs[register] = d
                table.source(register, export.source)
            for register, others in export.clashes.items():
                for other in others:
                    table.compare(register, defs[register], other, export.source)
    if nibepi:
        names["nibepi"] = SOURCE_NAMES["nibepi"]
        _apply_nibepi(table, nibepi)
    if extensions:
        names["nibe-lib-ext"] = SOURCE_NAMES["nibe-lib-ext"]
        _apply_extensions(table, extensions)
    if official:
        names["nibe-modbus-list"] = SOURCE_NAMES["nibe-modbus-list"]

    per_register: dict[int, dict[str, Definition]] = {}
    for product in sorted(table.defs):
        for register, d in table.defs[product].items():
            per_register.setdefault(register, {})[product] = d

    registers: dict[str, Any] = {}
    for register in sorted(per_register):
        by_product = per_register[register]
        base = _most_common(list(by_product.values()))
        sources = list(table.sources.get(register, {}))
        entry: dict[str, Any] = {
            f.name: getattr(base, f.name)
            for f in fields(Definition)
            if f.name not in ("title", "info")
        }
        named = _title(by_product.values())
        entry = {
            "title": named.title,
            "info": named.info or base.info,
            **entry,
            "sources": sources,
            "known": "documented" if any(documented(s) for s in sources) else "reported",
        }
        if register in table.mappings:
            entry["mappings"] = {str(k): v for k, v in sorted(table.mappings[register].items())}
        if register in table.disagreements:
            entry["disagreements"] = [
                {"source": s, "field": f, "value": v} for s, f, v in table.disagreements[register]
            ]
        if official and register in official:
            entry["official_name"] = official[register]
        variants = {
            product: {n: getattr(d, n) for n in VARIANT_FIELDS if getattr(d, n) != getattr(base, n)}
            for product, d in by_product.items()
            if d.key(VARIANT_FIELDS) != base.key(VARIANT_FIELDS)
        }
        if variants:
            entry["variants"] = variants
        registers[str(register)] = entry

    return {
        "format": FORMAT,
        "space": space,
        "sources": dict(sorted(names.items())),
        "registers": registers,
        "models": {p: sorted(table.defs[p]) for p in sorted(table.defs)},
    }
