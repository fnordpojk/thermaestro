"""The checks a plugin must pass, over either carrier.

The suite plays the core. It reads, subscribes and asks for series, but the only `act`
it sends goes to a lever that doesn't exist, unless the caller names levers to try: it is
meant to be safe against a real device.

A plugin in another language can be checked out of process:

    python -m thermaestro.cap.conformance --unix /path/to/socket

then start the plugin so it connects there.
"""

import argparse
import asyncio
import sys
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from functools import partial
from pathlib import Path
from typing import Any

from pydantic import TypeAdapter

from . import vocabulary
from .carrier import Closed, Endpoint
from .client import CapError, Link, UnexpectedReply
from .messages import (
    FINAL_STAGES,
    PROTOCOL,
    VERSION,
    Described,
    DeviceEvent,
    Fate,
    ForeignWrite,
    Health,
    Message,
    Op,
    RulesGet,
    SeriesData,
    SeriesGet,
    Update,
    Values,
    major,
)
from .model import Value

MISSING = "conformance.missing/x.conformance.none"
"""A path no plugin has."""


@dataclass(frozen=True, slots=True)
class Finding:
    check: str
    problem: str

    def __str__(self) -> str:
        return f"{self.check}: {self.problem}"


@dataclass(frozen=True, slots=True)
class Try:
    """A lever the suite may act on, and how."""

    lever: str
    op: Op
    params: dict[str, Value] = field(default_factory=dict)


async def run(
    endpoint: Endpoint,
    *,
    timeout_s: float = 5.0,
    quiet_s: float = 1.0,
    tries: Sequence[Try] = (),
) -> list[Finding]:
    """Check the plugin on `endpoint`; no findings means it conforms. `quiet_s` is how
    long a subscription must stay silent after it ends."""
    suite = _Suite(timeout_s, quiet_s)
    async with Link(endpoint, on_event=suite.events.append) as link:
        await suite.run(link, tries)
    return suite.findings


class _Problem(Exception):
    pass


class _Suite:
    def __init__(self, timeout_s: float, quiet_s: float) -> None:
        self.timeout_s = timeout_s
        self.quiet_s = quiet_s
        self.findings: list[Finding] = []
        self.events: list[Message] = []
        self.plugin = ""
        self.described = Described()

    async def run(self, link: Link, tries: Sequence[Try]) -> None:
        if not await self.check("hello", lambda: self.hello(link)):
            return  # nothing else can be trusted
        await self.check("hello.version", lambda: self.wrong_version(link))
        if not await self.check("describe", lambda: self.describe(link)):
            return
        await self.check("read", lambda: self.read(link))
        await self.check("read.unknown", lambda: self.read_unknown(link))
        await self.check("read.after", lambda: self.read_after(link))
        await self.check("act.unknown", lambda: self.act_unknown(link))
        for t in tries:
            await self.check(f"act {t.lever} {t.op}", partial(self.act, link, t))
        await self.check("subscribe", lambda: self.subscribe(link))
        await self.check("series", lambda: self.series(link))
        await self.check("unsupported", lambda: self.unsupported(link))
        await self.check("events", self.check_events)
        for bad in link.bad_lines:
            self.findings.append(Finding("lines", f"not a message: {bad.detail}"))

    async def check(self, name: str, body: Callable[[], Awaitable[None]]) -> bool:
        try:
            await body()
        except _Problem as e:
            self.findings.append(Finding(name, str(e)))
            return False
        except TimeoutError:
            self.findings.append(Finding(name, f"no answer within {self.timeout_s} s"))
            return False
        except (CapError, UnexpectedReply) as e:
            self.findings.append(Finding(name, f"answered with {e}"))
            return False
        except Closed:
            self.findings.append(Finding(name, "the plugin closed the connection"))
            return False
        return True

    # --- the checks ----------------------------------------------------------------------

    async def hello(self, link: Link) -> None:
        hello = await link.hello(timeout=self.timeout_s)
        _require(hello.role == "plugin", f"role {hello.role!r}, not 'plugin'")
        _require(hello.protocol == PROTOCOL, f"protocol {hello.protocol!r}")
        _require(major(hello.version) == major(VERSION), f"version {hello.version}")
        self.plugin = hello.plugin or ""  # a plugin's hello always names it

    async def wrong_version(self, link: Link) -> None:
        wrong = f"{int(major(VERSION)) + 1}.0"
        try:
            reply = await link.hello(version=wrong, timeout=self.timeout_s)
        except CapError as e:
            _require(e.code == "version", f"error code {e.code!r} for version {wrong}")
            return
        raise _Problem(f"accepted version {wrong} ({reply.type})")

    async def describe(self, link: Link) -> None:
        d = await link.describe(timeout=self.timeout_s)
        _require(d.complete, "the answer to describe isn't complete")
        self.described = d
        problems = list(_tree_problems(d, self.plugin))
        _require(not problems, "; ".join(problems))

    async def read(self, link: Link) -> None:
        points = [p.path for p in self.described.points]
        if not points:
            return
        values = await link.read(points, timeout=self.timeout_s)
        self._check_values(values.values, points)

    async def read_unknown(self, link: Link) -> None:
        values = await link.read([MISSING], timeout=self.timeout_s)
        self._check_values(values.values, [MISSING])
        v = values.values[0]
        _require(v.quality == "unknown", f"{MISSING} has quality {v.quality!r}, not 'unknown'")

    async def read_after(self, link: Link) -> None:
        points = [p.path for p in self.described.points]
        if not points:
            return
        after = datetime.now(UTC)
        values = await link.read(points, after=after, timeout=self.timeout_s)
        self._check_values(values.values, points)
        for v in values.values:
            _require(
                v.t_observed is None or v.t_observed >= after,
                f"{v.point} was observed at {v.t_observed}, before the read's `after`",
            )

    async def act_unknown(self, link: Link) -> None:
        fates = [f async for f in link.act(MISSING, "set", {"value": 0}, timeout=self.timeout_s)]
        _check_fates(fates)
        last = fates[-1]
        _require(last.stage == "dropped", f"ended {last.stage!r}, not 'dropped'")
        _require(bool(last.detail), "dropped without saying why")

    async def act(self, link: Link, t: Try) -> None:
        _require(
            any(lever.path == t.lever for lever in self.described.levers),
            "the lever isn't described",
        )
        _check_fates([f async for f in link.act(t.lever, t.op, t.params, timeout=self.timeout_s)])

    async def subscribe(self, link: Link) -> None:
        points = [p.path for p in self.described.points if p.delivery.how != "on_change"]
        if not points:
            return
        async with link.subscribe(points) as sub:
            update = await sub.next(self.timeout_s)
            for v in update.values:
                _require(v.point in points, f"an update for {v.point}, which wasn't asked for")
        await asyncio.sleep(self.quiet_s)
        late = [e for e in self.events if isinstance(e, Update) and e.id == sub.message.id]
        _require(not late, f"{len(late)} updates after unsubscribe")

    async def series(self, link: Link) -> None:
        start = datetime.now(UTC)
        end = start + timedelta(days=1)
        if not self.described.series:
            await _expect_unsupported(
                link.request(
                    SeriesGet(id=link.next_id(), series="none", start=start, end=end),
                    self.timeout_s,
                ),
                "series.get",
            )
        for info in self.described.series:
            reply = await link.request(
                SeriesGet(id=link.next_id(), series=info.id, start=start, end=end),
                self.timeout_s,
            )
            if not isinstance(reply, SeriesData):
                raise UnexpectedReply("series.get", reply)
            _require(reply.series == info.id, f"series.data for {reply.series}, not {info.id}")
            previous = None
            for i in reply.intervals:
                _require(i.series == info.id, f"an interval of {i.series} in {info.id}")
                _require(
                    previous is None or i.start >= previous,
                    f"{info.id}: intervals out of order or overlapping",
                )
                previous = i.end
        if not any(s.kind == "rule" for s in self.described.series):
            await _expect_unsupported(
                link.request(RulesGet(id=link.next_id(), scope="site"), self.timeout_s),
                "rules.get",
            )

    async def unsupported(self, link: Link) -> None:
        # A message meant for the core, sent to the plugin.
        await _expect_unsupported(
            link.request(Values(id=link.next_id(), values=()), self.timeout_s), "values"
        )

    async def check_events(self) -> None:
        units = {n.path for n in self.described.nodes}
        for e in self.events:
            if isinstance(e, Health | DeviceEvent | ForeignWrite) and e.unit is not None:
                _require(e.unit in units, f"{e.type} for {e.unit}, which isn't described")
            if isinstance(e, Described):
                _require(not e.complete and e.id is None, "a described sent unasked is partial")
            if isinstance(e, Fate):
                raise _Problem(f"a fate for {e.id}, which nothing asked for")

    def _check_values(self, values: Iterable[Any], asked: Sequence[str]) -> None:
        got = [v.point for v in values]
        _require(sorted(got) == sorted(asked), f"values for {got}, asked for {list(asked)}")
        units = {p.path: p.unit for p in self.described.points}
        for v in values:
            if v.point in units and v.value is not None:
                _require(
                    v.unit == units[v.point],
                    f"{v.point} in {v.unit!r}, described in {units[v.point]!r}",
                )


def _require(condition: bool, problem: str) -> None:
    if not condition:
        raise _Problem(problem)


async def _expect_unsupported(reply: Awaitable[Message], request: str) -> None:
    try:
        got = await reply
    except CapError as e:
        _require(e.code == "unsupported", f"{request}: error code {e.code!r}")
        return
    raise _Problem(f"{request} answered with {got.type}, not error 'unsupported'")


def _check_fates(fates: Sequence[Fate]) -> None:
    _require(bool(fates), "no fate")
    order = {"queued": 0, "sent": 1}
    seen = -1
    for f in fates[:-1]:
        _require(f.stage not in FINAL_STAGES, f"{f.stage!r} wasn't the last fate")
        _require(order[f.stage] > seen, f"{f.stage!r} out of order")
        seen = order[f.stage]
    _require(fates[-1].stage in FINAL_STAGES, f"no final fate, last {fates[-1].stage!r}")
    times = [f.t for f in fates]
    _require(times == sorted(times), "fate times go backward")


def _parent(path: str) -> str | None:
    return path.rsplit("/", 1)[0] if "/" in path else None


def _tree_problems(d: Described, plugin: str) -> Iterable[str]:
    kinds = {}
    for n in d.nodes:
        if n.path in kinds:
            yield f"node {n.path} twice"
        kinds[n.path] = n.kind
        parent = _parent(n.path)
        if parent is not None and parent not in {m.path for m in d.nodes}:
            yield f"node {n.path} has no parent node"
        if n.kind == "unit" and n.identity is None:
            yield f"unit {n.path} has no identity"
    paths = [p.path for p in d.points] + [lv.path for lv in d.levers]
    for path in {p for p in paths if paths.count(p) > 1}:
        yield f"{path} twice"
    problems: list[str] = []

    def placed(path: str) -> tuple[str, str] | None:
        """The node's kind and the local name, for a standard name; None otherwise."""
        node = _parent(path)
        name = path.rsplit("/", 1)[-1]
        if node is None or node not in kinds:
            problems.append(f"{path} isn't on a described node")
        elif name.startswith("x.") and not vocabulary.is_vendor(name, plugin):
            problems.append(f"{path} uses another plugin's namespace")
        elif not name.startswith("x."):
            return kinds[node], name
        return None

    for p in d.points:
        if (where := placed(p.path)) is None:
            continue
        kind, name = where
        std = vocabulary.point(kind, name)
        if std is None:
            problems.append(f"{name} isn't a standard point; use x.{plugin}.{name}")
        elif std.on and kind not in std.on:
            problems.append(f"{p.path}: {name} doesn't belong on a {kind}")
        elif std.unit is not None and p.unit != std.unit:
            problems.append(f"{p.path} is in {p.unit!r}, not {std.unit!r}")
    for lv in d.levers:
        if (where := placed(lv.path)) is None:
            continue
        kind, name = where
        standard = vocabulary.lever(kind, name)
        if standard is None:
            problems.append(f"{name} isn't a standard lever; use x.{plugin}.{name}")
        elif lv.kind not in standard.kinds:
            problems.append(f"{lv.path} is a {lv.kind}, not a {'/'.join(sorted(standard.kinds))}")
    yield from problems
    points = {p.path for p in d.points}
    for lever in d.levers:
        if lever.verify.point is not None and lever.verify.point not in points:
            yield f"{lever.path} verifies by {lever.verify.point}, which isn't described"
    ids = [s.id for s in d.series]
    if len(ids) != len(set(ids)):
        yield "a series id twice"


# --- out of process ------------------------------------------------------------------------

_TRIES = TypeAdapter(list[tuple[str, str, dict[str, Value]]])


async def _main(args: argparse.Namespace) -> int:
    from .sockets import listen_tcp, listen_unix

    connected: asyncio.Future[list[Finding]] = asyncio.get_running_loop().create_future()
    tries = [Try(lever, op, params) for lever, op, params in _TRIES.validate_json(args.tries)]  # type: ignore[arg-type]

    async def on_plugin(endpoint: Endpoint) -> None:
        if connected.done():
            return
        findings = await run(endpoint, timeout_s=args.timeout, tries=tries)
        connected.set_result(findings)

    if args.unix:
        server = await listen_unix(Path(args.unix), on_plugin)
    else:
        server = await listen_tcp(Path(args.tcp), on_plugin)
    async with server:
        sys.stdout.write("waiting for the plugin to connect\n")
        findings = await asyncio.wait_for(connected, args.wait)
    for f in findings:
        sys.stdout.write(f"FAIL {f}\n")
    sys.stdout.write("conforms\n" if not findings else f"{len(findings)} problems\n")
    return 0 if not findings else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Check that a plugin conforms.")
    where = parser.add_mutually_exclusive_group(required=True)
    where.add_argument("--unix", help="listen on this Unix socket")
    where.add_argument("--tcp", help="listen on TCP 127.0.0.1, writing this connection file")
    parser.add_argument("--timeout", type=float, default=5.0, help="seconds to wait per answer")
    parser.add_argument("--wait", type=float, default=60.0, help="seconds to wait for the plugin")
    parser.add_argument(
        "--tries",
        default="[]",
        help='levers to act on, as JSON: [["hp1/dhw/block", "engage", {}], ...]',
    )
    return asyncio.run(_main(parser.parse_args()))


if __name__ == "__main__":
    sys.exit(main())
