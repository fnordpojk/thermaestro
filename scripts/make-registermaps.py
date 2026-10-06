#!/usr/bin/env python3
"""Generate the Nibe register tables that ship with Thermaestro.

The sources stay outside the repo; give their folders:

    uv run python scripts/make-registermaps.py \\
        --modbusmanager DIR   # NIBE ModbusManager CSV exports
        --nibepi DIR          # NibePi's models/*.json
        --nibe-lib DIR        # the nibe library's nibe/data: S-series exports, extensions.json
        --official FILE       # Nibe's S-series register list, as `pdftotext -layout` gives it

Writes src/thermaestro/nibe/maps/data/bus.json and s-series.json. With --check, fails if
those differ from what the sources give.
"""

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

from thermaestro.nibe.maps.build import (
    ExtBlock,
    SourceModel,
    build,
    read_extensions,
    read_modbusmanager,
    read_nibepi,
    read_official_list,
    read_s_export,
)

OUT = Path(__file__).resolve().parent.parent / "src" / "thermaestro" / "nibe" / "maps" / "data"
S_SERIES = re.compile(r"^(S\d|VVMS|SMOS)")
"""NibePi's S-series files; the rest are bus models. RMU40_* are the room unit's own values."""


def dumps(table: dict[str, Any]) -> str:
    """JSON with one register and one model per line, so a regenerated table diffs readably."""

    def one(value: object) -> str:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))

    def block(name: str, items: dict[str, Any], last: bool) -> list[str]:
        lines = [f' "{name}": {{']
        rows = list(items.items())
        lines += [
            f"  {one(k)}: {one(v)}{',' if i < len(rows) - 1 else ''}"
            for i, (k, v) in enumerate(rows)
        ]
        return [*lines, " }" if last else " },"]

    head = [f' "{k}": {one(table[k])},' for k in ("format", "space", "sources")]
    return "\n".join(
        [
            "{",
            *head,
            *block("registers", table["registers"], last=False),
            *block("models", table["models"], last=True),
            "}",
            "",
        ]
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--modbusmanager", type=Path, required=True)
    ap.add_argument("--nibepi", type=Path, required=True)
    ap.add_argument("--nibe-lib", type=Path, required=True)
    ap.add_argument("--official", type=Path)
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args()

    exports = [
        read_modbusmanager(p.read_bytes(), p.stem) for p in sorted(args.modbusmanager.glob("*.csv"))
    ]
    nibepi_bus: list[SourceModel] = []
    nibepi_s: list[SourceModel] = []
    for p in sorted(args.nibepi.glob("*.json")):
        if p.stem.startswith("RMU40"):
            continue
        (nibepi_s if S_SERIES.match(p.stem) else nibepi_bus).append(
            read_nibepi(p.read_bytes(), p.stem)
        )
    s_exports = []
    for p in sorted(args.nibe_lib.glob("*.csv")):
        data = p.read_bytes()
        if b"Register type" in data.split(b"\n", 1)[0]:
            s_exports.append(read_s_export(data, p.stem))
    ext_file = args.nibe_lib / "extensions.json"
    extensions: list[ExtBlock] = read_extensions(ext_file.read_bytes()) if ext_file.exists() else []
    official = read_official_list(args.official.read_text()) if args.official else None

    tables = {
        "bus": build("bus", modbusmanager=exports, nibepi=nibepi_bus, extensions=extensions),
        "s-series": build(
            "s-series",
            s_exports=s_exports,
            nibepi=nibepi_s,
            extensions=extensions,
            official=official,
        ),
    }
    stale = []
    for space, table in tables.items():
        text = dumps(table)
        path = OUT / f"{space}.json"
        sys.stdout.write(
            f"{space}: {len(table['registers'])} registers, {len(table['models'])} models, "
            f"sources {', '.join(table['sources'])}\n"
        )
        if args.check:
            if not path.exists() or path.read_text(encoding="utf-8") != text:
                stale.append(path.name)
        else:
            OUT.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
    if stale:
        sys.stderr.write(f"out of date: {', '.join(stale)}\n")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
