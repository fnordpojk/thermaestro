"""The register tables: which registers each Nibe model has, and what each one is.

There are two tables, one per register space: `bus` for the models on the MODBUS40 accessory
bus (F-series, VVM, SMO, MHB) and `s-series` for the S-series over Modbus TCP, numbered as
input register n = 3nnnn and holding register n = 4nnnn. The spaces are separate because the
same numbers mean different things in them.

A register has one definition, except where a model's own export says otherwise; that model
then gets its own variant, and `ModelMap.register` returns it.
"""

import json
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field, replace
from enum import Enum
from importlib import resources
from pathlib import Path
from typing import Any


class Size(Enum):
    S8 = "s8"
    U8 = "u8"
    S16 = "s16"
    U16 = "u16"
    S32 = "s32"
    U32 = "u32"

    @property
    def bits(self) -> int:
        return int(self.value[1:])

    @property
    def signed(self) -> bool:
        return self.value[0] == "s"


@dataclass(frozen=True, slots=True)
class Disagreement:
    """What another source says about a field, where it differs from the definition used."""

    source: str
    field: str
    value: str


@dataclass(frozen=True, slots=True)
class Register:
    id: int
    title: str
    info: str
    unit: str
    size: Size | None
    """None where no source gives the size: such a register is never decoded."""
    factor: int
    min: int | None
    max: int | None
    """Raw limits, before the factor; None where the sources give no range (Nibe's 0/0)."""
    default: int | None
    writable: bool
    sources: tuple[str, ...]
    known: str
    """"documented" where a vendor export lists the register, "reported" where only NibePi's
    files or the nibe library's corrections do. Reported knowledge may make Thermaestro more
    careful, never less."""
    mappings: dict[int, str] | None = field(default=None, hash=False)
    """Names for values, where a source gives them."""
    disagreements: tuple[Disagreement, ...] = ()
    official_name: str | None = None
    """The name in Nibe's S-series register list, where it names the register."""


@dataclass(frozen=True, slots=True)
class ModelMap:
    name: str
    ids: frozenset[int]
    table: "RegisterMap" = field(repr=False)

    def register(self, register: int) -> Register:
        if register not in self.ids:
            raise KeyError(f"{self.name} has no register {register}")
        return self.table.register(register, model=self.name)

    def __contains__(self, register: object) -> bool:
        return register in self.ids

    def __iter__(self) -> Iterator[int]:
        return iter(sorted(self.ids))


class RegisterMap:
    def __init__(
        self,
        space: str,
        sources: Mapping[str, str],
        registers: Mapping[int, Register],
        variants: Mapping[int, Mapping[str, Register]],
        members: Mapping[str, frozenset[int]],
    ) -> None:
        self.space = space
        self.sources = dict(sources)
        self._registers = dict(registers)
        self._variants = {r: dict(v) for r, v in variants.items()}
        self.models = {name: ModelMap(name, ids, self) for name, ids in members.items()}

    def model(self, name: str) -> ModelMap:
        try:
            return self.models[name]
        except KeyError:
            raise KeyError(f"no {self.space} register table for model {name!r}") from None

    def register(self, register: int, *, model: str | None = None) -> Register:
        """The register's definition; with `model`, that model's variant where it has one."""
        if model is not None:
            variant = self._variants.get(register, {}).get(model)
            if variant is not None:
                return variant
        return self._registers[register]


def _register(rid: int, d: Mapping[str, Any]) -> Register:
    return Register(
        id=rid,
        title=d["title"],
        info=d["info"],
        unit=d["unit"],
        size=Size(d["size"]) if d["size"] is not None else None,
        factor=d["factor"],
        min=d["min"],
        max=d["max"],
        default=d["default"],
        writable=d["writable"],
        sources=tuple(d["sources"]),
        known=d["known"],
        mappings={int(k): v for k, v in d["mappings"].items()} if d.get("mappings") else None,
        disagreements=tuple(Disagreement(**x) for x in d.get("disagreements", ())),
        official_name=d.get("official_name"),
    )


def _variant(base: Register, fields: Mapping[str, Any]) -> Register:
    changed = dict(fields)
    if "size" in changed:
        changed["size"] = Size(changed["size"]) if changed["size"] is not None else None
    return replace(base, **changed)


def from_json(data: Mapping[str, Any]) -> RegisterMap:
    if data.get("format") != 1:
        raise ValueError(f"unknown register map format {data.get('format')!r}")
    registers: dict[int, Register] = {}
    variants: dict[int, dict[str, Register]] = {}
    for key, d in data["registers"].items():
        rid = int(key)
        registers[rid] = _register(rid, d)
        for model, fields in d.get("variants", {}).items():
            variants.setdefault(rid, {})[model] = _variant(registers[rid], fields)
    members = {name: frozenset(ids) for name, ids in data["models"].items()}
    return RegisterMap(data["space"], data["sources"], registers, variants, members)


def load(space: str) -> RegisterMap:
    """One of the tables that ship with Thermaestro: "bus" or "s-series"."""
    text = (
        resources.files(__package__).joinpath("data", f"{space}.json").read_text(encoding="utf-8")
    )
    return from_json(json.loads(text))


def load_file(path: Path) -> RegisterMap:
    """A table a user generated from their own export."""
    return from_json(json.loads(path.read_text(encoding="utf-8")))
