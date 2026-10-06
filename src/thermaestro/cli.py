"""The `thermaestro` command."""

import argparse
import asyncio
import logging
import sys
from pathlib import Path

from .core import run, verify
from .store import Layout, StoreError, load_startup


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="thermaestro")
    parser.add_argument(
        "--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"]
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("run", help="run Thermaestro")
    audit = commands.add_parser("audit", help="the audit log")
    audit_commands = audit.add_subparsers(dest="audit_command", required=True)
    check = audit_commands.add_parser("verify", help="check the audit log's chain")
    check.add_argument("file", nargs="?", type=Path, help="default: the state directory's")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=args.log_level, format="%(levelname)s %(name)s: %(message)s", stream=sys.stderr
    )
    layout = Layout.from_environment()
    try:
        layout = load_startup(layout.startup).layout(layout)
        if args.command == "run":
            asyncio.run(run(layout))
            return 0
        return _verify(args.file or layout.audit / "audit.jsonl")
    except StoreError as e:
        sys.stderr.write(f"{e}\n")
        return 2


def _verify(path: Path) -> int:
    problems = verify(path)
    for problem in problems:
        sys.stdout.write(f"{problem}\n")
    sys.stdout.write("intact\n" if not problems else f"{len(problems)} problems\n")
    return 0 if not problems else 1


if __name__ == "__main__":
    sys.exit(main())
