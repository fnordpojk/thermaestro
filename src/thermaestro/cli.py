"""The `thermaestro` command."""

import argparse
import asyncio
import getpass
import json
import logging
import os
import sys
import urllib.error
import urllib.request
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from .auth import AccountError, Accounts, SetupCode
from .core import AuditLog, run, verify
from .files import private_directory
from .store import Database, Layout, StoreError, load_startup


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
    logset = commands.add_parser(
        "nibe-logset", help="write a LOG.SET for a Nibe bus-family pump, to copy to a USB stick"
    )
    logset.add_argument("model", help="as the register map names it, e.g. F1245")
    logset.add_argument("output", type=Path, help="the file to write, e.g. LOG.SET")
    commands.add_parser(
        "setup-code", help="print the code for creating the first administrator in the web UI"
    )
    admin = commands.add_parser("admin", help="users, from the host")
    admin_commands = admin.add_subparsers(dest="admin_command", required=True)
    create = admin_commands.add_parser("create", help="add a user; asks for the password")
    create.add_argument("name")
    create.add_argument(
        "--group", action="append", help="default: Administrators; may be given again"
    )
    reset = admin_commands.add_parser(
        "reset-password", help="set a user's password, and enable the user if disabled"
    )
    reset.add_argument("name")
    status = commands.add_parser(
        "status",
        help="what Thermaestro reads, through its API; an API token in THERMAESTRO_TOKEN",
    )
    status.add_argument("--url", default="http://127.0.0.1:8080", help="the web UI's address")
    probe = commands.add_parser(
        "probe",
        help="read a Nibe pump through its gateway, writing nothing to it; makes a report"
        " and a bus capture to send with a new model or a bug",
    )
    probe.add_argument("host", help="the gateway's address")
    probe.add_argument("--protocol", choices=["nibegw", "thermaestro-gw"], default="nibegw")
    probe.add_argument(
        "--key-file", type=Path, help="Thermaestro gateway protocol: a file with its 64-digit key"
    )
    probe.add_argument("--read-port", type=int, default=9999)
    probe.add_argument("--control-port", type=int, default=10090)
    probe.add_argument(
        "--local-port", type=int, default=0, help="where to listen for the gateway; 0 picks one"
    )
    probe.add_argument("--model", help="the model, if the pump's own name isn't recognized")
    probe.add_argument("--minutes", type=float, default=5.0, help="how long to capture the bus")
    probe.add_argument("--out", type=Path, default=Path(), help="the directory to write to")
    args = parser.parse_args(argv)
    if args.command == "nibe-logset":
        return _logset(args.model, args.output)
    if args.command == "status":
        return _status(args.url, os.environ.get("THERMAESTRO_TOKEN", ""))

    logging.basicConfig(
        level=args.log_level, format="%(levelname)s %(name)s: %(message)s", stream=sys.stderr
    )
    if args.command == "probe":
        return _probe(args)
    layout = Layout.from_environment()
    try:
        layout = load_startup(layout.startup).layout(layout)
        if args.command == "run":
            asyncio.run(run(layout))
            return 0
        if args.command == "setup-code":
            return asyncio.run(_setup_code(layout))
        if args.command == "admin":
            return asyncio.run(_admin(layout, args))
        return _verify(args.file or layout.audit / "audit.jsonl")
    except StoreError as e:
        sys.stderr.write(f"{e}\n")
        return 2


def _logset(model_name: str, output: Path) -> int:
    from .nibe import logset, profile
    from .nibe.maps import load

    try:
        model = load("bus").model(model_name)
    except KeyError as e:
        sys.stderr.write(f"{e.args[0]}\n")
        return 2
    registers = [r for r in profile.LOG_SET if r in model]
    output.write_bytes(logset.render(model, registers, day=date.today()))
    sys.stdout.write(f"wrote {output}: {len(registers)} registers\n")
    return 0


def _probe(args: argparse.Namespace) -> int:
    from .nibe.probe import PSK_NAME, ProbeFailed, probe
    from .store import NibeGateway

    psk = None
    if args.key_file is not None:
        try:
            psk = bytes.fromhex(args.key_file.read_text().strip())
        except (OSError, ValueError) as e:
            sys.stderr.write(f"the key file can't be read as 64 hex digits: {e}\n")
            return 2
        if len(psk) != 32:
            sys.stderr.write("the key is 64 hex digits\n")
            return 2
    if args.protocol == "thermaestro-gw" and psk is None:
        sys.stderr.write("the Thermaestro gateway protocol needs --key-file\n")
        return 2
    gateway = NibeGateway(
        host=args.host,
        protocol=args.protocol,
        read_port=args.read_port,
        control_port=args.control_port,
        local_port=args.local_port,
        psk=PSK_NAME if psk is not None else None,
        model=args.model,
    )

    def say(text: str) -> None:
        sys.stdout.write(f"{text}\n")
        sys.stdout.flush()

    try:
        result = asyncio.run(probe(gateway, psk=psk, seconds=args.minutes * 60, progress=say))
    except ProbeFailed as e:
        sys.stderr.write(f"{e}\n")
        return 2
    except OSError as e:
        sys.stderr.write(f"the gateway can't be reached: {e.strerror or e}\n")
        return 2
    report = result.report
    pump = report["pump"]
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M")
    args.out.mkdir(parents=True, exist_ok=True)
    report_path = args.out / f"probe-{pump['model']}-{stamp}.json"
    capture_path = args.out / f"probe-{pump['model']}-{stamp}-capture.jsonl"
    report_path.write_text(json.dumps(report, indent=1, ensure_ascii=False) + "\n")
    header = {"made": report["made"], **pump, "protocol": report["transport"]["protocol"]}
    capture_path.write_text(result.capture.text(header))
    points = report["points"]
    good = sum(1 for p in points if p["quality"] == "good")
    say("Nothing was written to the pump.")
    say(
        f"{good} of {len(points)} points read with a good value;"
        f" {len(report['missing'])} of Thermaestro's points aren't in this model's map;"
        f" {len(report['levers'])} levers offered, {len(report['levers_missing'])} not."
    )
    say(f"Wrote {report_path} and {capture_path}.")
    say(
        "The capture holds everything on the bus, other devices' traffic too."
        " Look through both files before sending them."
    )
    return 0


async def _setup_code(layout: Layout) -> int:
    private_directory(layout.state)
    async with await Database.open(layout.database) as db:
        if await Accounts(db, AuditLog(layout.audit)).has_admin():
            sys.stderr.write("there is already an administrator; see `thermaestro admin`\n")
            return 1
    setup = SetupCode(layout.setup_code)
    code = setup.current() or setup.issue()
    sys.stdout.write(f"{code}\n")
    return 0


async def _admin(layout: Layout, args: argparse.Namespace) -> int:
    private_directory(layout.state)
    async with await Database.open(layout.database) as db:
        accounts = Accounts(db, AuditLog(layout.audit))
        if await accounts.user(args.name) is None and args.admin_command == "reset-password":
            sys.stderr.write(f"no user {args.name!r}\n")
            return 1
        password = await asyncio.to_thread(getpass.getpass, "Password: ")
        if await asyncio.to_thread(getpass.getpass, "The same again: ") != password:
            sys.stderr.write("the two passwords differ\n")
            return 1
        try:
            if args.admin_command == "create":
                groups = args.group or ["Administrators"]
                await accounts.create_user(args.name, password, groups, by="cli")
                sys.stdout.write(f"created {args.name} in {', '.join(groups)}\n")
            else:
                question = f"Also log {args.name} out everywhere? [Y/n] "
                answer = (await asyncio.to_thread(input, question)).strip().lower()
                end = answer in ("", "y", "yes")
                await accounts.set_password(args.name, password, by="cli", end_sessions=end)
                sys.stdout.write(f"set the password for {args.name}\n")
        except AccountError as e:
            sys.stderr.write(f"{e}\n")
            return 1
    return 0


def _status(url: str, token: str) -> int:
    """Print each plugin instance and its values, as the API gives them."""
    if not token:
        sys.stderr.write("set THERMAESTRO_TOKEN to an API token (made on your account page)\n")
        return 2
    if not url.startswith(("http://", "https://")):
        sys.stderr.write("the URL starts with http:// or https://\n")
        return 2
    request = urllib.request.Request(  # noqa: S310 - the scheme is checked above
        url.rstrip("/") + "/api/v1/status", headers={"Authorization": f"Bearer {token}"}
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as answer:  # noqa: S310
            instances: list[dict[str, Any]] = json.load(answer)
    except urllib.error.HTTPError as e:
        sys.stderr.write(f"{url}: {e.code} {e.reason}\n")
        return 1
    except (urllib.error.URLError, OSError) as e:
        sys.stderr.write(f"{url}: {e}\n")
        return 1
    for instance in instances:
        sys.stdout.write(f"{instance['id']} ({instance['plugin']}): {instance['state']}\n")
        for p in instance["points"]:
            value = "-" if p["value"] is None else f"{p['value']}"
            unit = f" {p['unit']}" if p["unit"] and p["value"] is not None else ""
            quality = "" if p["quality"] == "good" else f"  [{p['quality']}"
            quality += f": {p['why']}]" if quality and p["why"] else ("]" if quality else "")
            sys.stdout.write(f"  {p['path']}  {value}{unit}{quality}\n")
    return 0


def _verify(path: Path) -> int:
    problems = verify(path)
    for problem in problems:
        sys.stdout.write(f"{problem}\n")
    sys.stdout.write("intact\n" if not problems else f"{len(problems)} problems\n")
    return 0 if not problems else 1


if __name__ == "__main__":
    sys.exit(main())
