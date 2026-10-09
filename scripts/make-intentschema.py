#!/usr/bin/env python3
"""Write the intents' JSON Schema from the types; with --check, fail if the checked-in copy
differs."""

import argparse
import sys
from pathlib import Path

from thermaestro.intents import schema

TARGET = Path(__file__).parent.parent / "src/thermaestro/intents/data" / schema.FILE


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    text = schema.dumps(schema.generate())
    if args.check:
        if not TARGET.exists() or TARGET.read_text(encoding="utf-8") != text:
            sys.stderr.write(f"{TARGET} is out of date; run {Path(__file__).name}\n")
            return 1
        return 0
    TARGET.parent.mkdir(exist_ok=True)
    TARGET.write_text(text, encoding="utf-8")
    sys.stdout.write(f"wrote {TARGET}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
