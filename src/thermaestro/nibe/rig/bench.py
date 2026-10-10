"""The rig's bench: the core in miniature, for one pump, and what a check records.

The Nibe plugin runs under the plugin host, its values are kept, and the executor takes
levers over, as in the daemon; the database, the audit log and the plugin's own store (the
hot-water block's release) live in the bench's state directory. Without `write`, the
plugin's transport has no way to write and the levers are in shadow.
"""

import asyncio
import contextlib
import tempfile
import threading
from collections.abc import Awaitable, Callable, Collection, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from ... import clock, durations, version
from ...cap.client import CapError
from ...cap.messages import Op
from ...cap.model import Envelope, Lever, Value
from ...core import AuditLog, Executor, Key, PluginContext, PluginHost, Result, State, Values
from ...core.executor import TAKEN, left_changed, reading
from ...files import private_directory
from ...store import Control, Database, NibeGateway, Plugin, SecretStore
from .. import profile
from ..plugin import NibePlugin
from ..probe import PSK_NAME, ReadOnly
from ..transport import GatewayConfig, ModbusConfig, connect
from ..transport.base import Transport

FORMAT = "thermaestro-rig"
VERSION = 1
PUMP = "pump"
WHO = "rig"
"""The principal of the rig's requests: a person at the command line."""
READY_S = 600.0
"""How long the bench waits for the values a check needs: on a real pump, polled registers
come about one a second."""
TICK_S = 60.0
"""How often a watch keeps how things stand."""
SAY_S = 600.0
"""How often it says so, when nothing changes."""
POLL_S = 2.0
FINE = frozenset({"verified", "awaiting_effect", "shadowed", "unchanged"})
"""What a request may come to for a check to go on."""
PUT_BACK = TAKEN | {"unchanged"}

Ask = Callable[[str], Awaitable[bool | None]]
"""A yes-or-no question to the person at the rig; None when it wasn't answered."""
Say = Callable[[str], None]


class RigError(Exception):
    """Why a check can't run."""


class Declined(Exception):
    """The person said no before anything was written."""


@dataclass
class Options:
    minutes: float = 240.0
    """The longest a check waits for the pump to do something by itself."""
    system: int = 1
    """The climate system whose offset `offset` tries."""
    mode: str | None = None
    """The hot-water mode `mode` changes to; by default a neighbor of the current one."""
    setting: Literal["stop_temp", "max_power"] = "stop_temp"
    """The addition's setting `addition` tries."""
    pool: int = 1


@dataclass
class Step:
    what: str
    outcome: str
    detail: str | None = None
    passed: bool | None = None
    """Whether it went as it should; None where there is nothing to judge."""
    t: float = 0.0
    """Seconds from the start of the run."""


@dataclass
class Report:
    check: str
    wrote: bool
    made: str
    pump: dict[str, Any]
    options: dict[str, Any]
    steps: list[Step] = field(default_factory=list)
    timeline: list[dict[str, Any]] = field(default_factory=list)
    judging: bool = False
    """Whether the check asks a person "as expected?"."""
    judged: bool | None = None
    """The answer."""
    levers: tuple[str, ...] = ()

    @property
    def failed(self) -> bool:
        return self.judged is False or any(s.passed is False for s in self.steps)

    @property
    def evidence(self) -> dict[str, Any] | None:
        """What this run shows of the levers, where it wrote and everything went as it
        should: a lever's works may be marked verified on this model and firmware."""
        acted = [s for s in self.steps if s.outcome in TAKEN]
        if not self.wrote or self.failed or not acted or self.check == "restore":
            return None
        if self.judging and self.judged is not True:
            return None
        return {
            "levers": list(self.levers),
            "model": self.pump.get("model"),
            "firmware": self.pump.get("firmware"),
            "made": self.made,
        }

    def log(self) -> dict[str, Any]:
        return {
            "format": FORMAT,
            "version": VERSION,
            "thermaestro": version(),
            "made": self.made,
            "check": self.check,
            "wrote": self.wrote,
            "pump": self.pump,
            "options": self.options,
            "levers": list(self.levers),
            "steps": [asdict(s) for s in self.steps],
            "judged": self.judged,
            "evidence": self.evidence,
            "timeline": self.timeline,
        }

    def table(self) -> str:
        pump = self.pump
        how = "wrote to the pump" if self.wrote else "read-only: nothing was written"
        head = f"{self.check} on {pump.get('model')}, firmware {pump.get('firmware')}: {how}"
        rows = [(str(i), s.what, s.outcome, _mark(s.passed)) for i, s in enumerate(self.steps, 1)]
        widths = [max([len(r[c]) for r in rows] + [1]) for c in range(3)]
        lines = [head]
        for row in rows:
            lines.append(
                f"{row[0]:>{widths[0]}}  {row[1]:<{widths[1]}}  {row[2]:<{widths[2]}}  {row[3]}"
            )
            step = self.steps[int(row[0]) - 1]
            if step.detail:
                lines.append(f"{'':>{widths[0]}}  ({step.detail})")
        if self.evidence is not None:
            verb = "works" if len(self.levers) == 1 else "work"
            lines.append(
                f"Every step went as it should: evidence that {', '.join(self.levers)} {verb}"
                " on this model and firmware."
            )
        elif self.failed:
            lines.append("Something didn't go as it should: see the steps marked FAILED.")
        return "\n".join(lines) + "\n"


def _mark(passed: bool | None) -> str:
    return {True: "ok", False: "FAILED", None: "-"}[passed]


class Bench:
    def __init__(
        self,
        gateway: NibeGateway,
        *,
        state: Path,
        write: bool,
        say: Say,
        ask: Ask,
        psk: bytes | None = None,
        connect_fn: Callable[..., Awaitable[Transport]] = connect,
        transport_settings: dict[str, Any] | None = None,
        identify_timeout_s: float = 40.0,
        ready_s: float = READY_S,
        plugin_options: Mapping[str, Any] | None = None,
    ) -> None:
        self.gateway = gateway
        self._plugin_options = dict(plugin_options or {})
        self.state = state
        self.write = write
        self.say = say
        self._ask = ask
        self._psk = psk
        self._connect_fn = connect_fn
        self._transport_settings = transport_settings
        self._identify_timeout_s = identify_timeout_s
        self._ready_s = ready_s
        self.steps: list[Step] = []
        self.timeline: list[dict[str, Any]] = []
        self.judging = False
        self.judged: bool | None = None
        self.plugin: NibePlugin | None = None
        self._started = clock.monotonic()
        self._db: Database | None = None
        self._host: PluginHost | None = None
        self._executor: Executor | None = None
        self._running = False
        self._reported: set[str] = set()
        self._tmp: tempfile.TemporaryDirectory[str] | None = None

    # --- starting and stopping -------------------------------------------------------------

    async def open(self) -> list[str]:
        """Connect and identify the pump. Returns the levers an earlier run left changed."""
        private_directory(self.state)
        self._db = db = await Database.open(self.state / "rig.db")
        self._tmp = tempfile.TemporaryDirectory()
        secrets = SecretStore(Path(self._tmp.name) / "secrets.json")
        gateway = self.gateway
        if self._psk is not None:
            await secrets.set(PSK_NAME, self._psk.hex())
            gateway = gateway.model_copy(update={"psk": PSK_NAME})
        settings = gateway.model_dump(mode="json", exclude_none=True)
        await db.put(Plugin(plugin="nibe", settings=settings), PUMP)
        audit = AuditLog(self.state / "audit")
        self.values = Values(db)

        def nibe(context: PluginContext) -> NibePlugin:
            self.plugin = NibePlugin(
                gateway,
                secrets=secrets,
                state=context.state,
                connect_fn=self._connect,
                transport_settings=self._transport_settings,
                identify_timeout_s=self._identify_timeout_s,
                **self._plugin_options,
            )
            return self.plugin

        self._host = PluginHost(
            db=db,
            secrets=secrets,
            values=self.values,
            audit=audit,
            factories={"nibe": nibe},
            describe_timeout_s=self._identify_timeout_s + 30,
        )
        self._executor = Executor(db, self._host, self.values, audit)
        await self._host.start()
        until = clock.monotonic() + self._identify_timeout_s + 60
        while self._instance().state is not State.UP:
            problem = self.plugin.problem if self.plugin is not None else None
            if problem is not None:
                raise RigError(problem)
            if clock.monotonic() > until:
                why = self._instance().last_error
                raise RigError(f"the pump wasn't identified{f' ({why})' if why else ''}")
            await asyncio.sleep(0.2)
        return await left_changed(db)

    async def _connect(self, config: GatewayConfig | ModbusConfig, **settings: Any) -> Transport:
        transport = await self._connect_fn(config, **settings)
        return transport if self.write else ReadOnly(transport)

    async def ready(self, levers: Collection[str], points: Collection[str]) -> None:
        """Wait until the pump has given every value the check needs: its own, and what the
        levers read back and rest on."""
        needed = {f"{profile.UNIT}/{p}" for p in points}
        for path in levers:
            lever = self.need(path)
            if lever.verify.point is not None:
                needed.add(lever.verify.point)
            needed.update(c.split()[0] for c in lever.preconditions.value or ())
        until = clock.monotonic() + self._ready_s
        next_say = clock.monotonic() + TICK_S
        while missing := sorted(p for p in needed if not self._read(p)):
            if clock.monotonic() > until:
                raise RigError(f"these weren't read: {', '.join(missing)}")
            if clock.monotonic() >= next_say:
                got = len(needed) - len(missing)
                self.say(f"Waiting for the pump's values: {got} of {len(needed)} read.")
                next_say += TICK_S
            await asyncio.sleep(1.0)

    def _read(self, point: str) -> bool:
        found = self.values.latest.get(Key(PUMP, point))
        return found is not None and found.why != "not read yet"

    async def consent(self, writes: Sequence[str], levers: Collection[str]) -> None:
        """Say what the check writes, have the levers' competing features confirmed off, ask
        before the first change, and hand the levers to the executor: in control with
        `write`, else in shadow."""
        names = sorted({f.name for p in levers for f in self.need(p).competing_features})
        confirmed = {name for name in names if await self.ask(f"Is {name} switched off?")}
        if self.write:
            self.say("This check writes to the pump:")
            for line in writes:
                self.say(f"  - {line}")
            self.say(
                "Each is put back as it was found when the check ends, or if it stops early."
                " If Thermaestro itself runs against this pump, keep these levers off or in"
                " shadow there meanwhile."
            )
            if not await self.ask("Go ahead?"):
                raise Declined
        else:
            self.say(
                "Read-only: every step before a write runs, and nothing is written. It would write:"
            )
            for line in writes:
                self.say(f"  - {line}")
        mode = "control" if self.write else "shadow"
        await self.db.put(
            Control(
                levers={self.ref(p): mode for p in levers},
                confirmed_off={
                    self.ref(p): tuple(
                        f.name for f in self.need(p).competing_features if f.name in confirmed
                    )
                    for p in levers
                },
                min_hold_s=0.0,  # the rig changes a lever and puts it back at once
            )
        )
        await self.start_executor()

    async def start_executor(self) -> None:
        await self.executor.start()
        self._running = True

    async def close(self) -> None:
        """Put back what is still changed, forget what was taken over, and stop."""
        try:
            if self._running:
                results = await self.executor.restore("the rig is stopping")
                for ref, result in results.items():
                    self.put_back_step(ref, result)
                self._let_go()
                await self.executor.forget()
                await self.executor.stop()
        finally:
            self._running = False
            if self._host is not None:
                await self._host.stop()
            if self._db is not None:
                await self._db.close()
            if self._tmp is not None:
                self._tmp.cleanup()

    # --- what a check uses -----------------------------------------------------------------

    @property
    def db(self) -> Database:
        assert self._db is not None  # noqa: S101 - opened first
        return self._db

    @property
    def executor(self) -> Executor:
        assert self._executor is not None  # noqa: S101 - opened first
        return self._executor

    def _instance(self) -> Any:
        assert self._host is not None  # noqa: S101 - opened first
        return self._host.instances[PUMP]

    def pump(self) -> dict[str, Any]:
        plugin, described = self.plugin, self._instance().described
        return {
            "family": plugin.family.name if plugin else None,
            "product": described.nodes[0].label if described and described.nodes else None,
            "model": plugin.model.name if plugin and plugin.model else None,
            "firmware": plugin.firmware if plugin else None,
        }

    @staticmethod
    def ref(path: str) -> str:
        return f"{PUMP}:{profile.UNIT}/{path}"

    def lever(self, path: str) -> Lever | None:
        return self.executor.lever(self.ref(path))

    def need(self, path: str) -> Lever:
        """A lever this pump offers, or why the check can't run."""
        lever = self.lever(path)
        if lever is None:
            raise RigError(f"this pump has no {path}")
        return lever

    def envelope(self, point: str) -> Envelope | None:
        return self.values.latest.get(Key(PUMP, f"{profile.UNIT}/{point}"))

    def value(self, point: str) -> Any:
        """A point's latest value, where it is good."""
        found = self.envelope(point)
        return found.value if found is not None and found.quality == "good" else None

    def setting(self, path: str) -> Value | None:
        """A setting lever's value now, in the lever's terms."""
        lever = self.need(path)
        if lever.verify.point is None:
            return None
        found = self.values.latest.get(Key(PUMP, lever.verify.point))
        if found is None or found.quality != "good":
            return None
        return reading(found, lever.params.get("value"))

    async def fresh(self, point: str) -> Any:
        """A point's value from a read the pump answers from now on."""
        link = self._instance().link
        if link is None:
            return None
        with contextlib.suppress(TimeoutError, CapError):
            answer = await link.read([f"{profile.UNIT}/{point}"], after=clock.now(), timeout=60)
            found = answer.values[0]
            return found.value if found.quality == "good" else None
        return None

    async def act(
        self, path: str, op: Op, params: Mapping[str, Value] | None = None, *, why: str
    ) -> Result:
        """Ask the executor, as Thermaestro asks it in control, and record what became of it."""
        result = await self.executor.act(self.ref(path), op, dict(params or {}), who=WHO, why=why)
        value = (params or {}).get("value")
        what = f"{op} {path}" + ("" if value is None else f" to {_text(value)}")
        self.step(what, result.outcome, result.detail, result.outcome in FINE)
        return result

    async def put_back(self, paths: Collection[str]) -> None:
        """Put levers back as they were found, as Thermaestro does on its way out."""
        refs = [self.ref(p) for p in paths]
        if not self.write:
            for path in paths:
                claim = self.executor.claims.get(self.ref(path))
                if claim is not None and claim.changed:
                    self.step(f"put back {path}", "shadowed", "nothing was changed", True)
        results = await self.executor.restore("the rig's check is done", refs)
        for ref, result in results.items():
            self.put_back_step(ref, result)
        self._let_go(refs)

    def _let_go(self, refs: Collection[str] | None = None) -> None:
        """Say which levers were let go after a change the rig didn't make: those aren't
        put back, as Thermaestro never writes over someone else's change."""
        for ref, claim in self.executor.claims.items():
            if claim.drift is None or ref in self._reported or (refs and ref not in refs):
                continue
            self._reported.add(ref)
            path = ref.removeprefix(f"{PUMP}:{profile.UNIT}/")
            self.step(f"{path} was changed by something else", "let go", claim.drift, False)

    def put_back_step(self, ref: str, result: Result) -> None:
        path = ref.removeprefix(f"{PUMP}:{profile.UNIT}/")
        # A hold's release awaits no effect of its own: the expectation is the engage's.
        detail = None if result.outcome == "awaiting_effect" else result.detail
        self.step(f"put back {path}", result.outcome, detail, result.outcome in PUT_BACK)

    async def watch(
        self, until: Callable[[], bool], *, minutes: float, show: Mapping[str, str]
    ) -> float | None:
        """Wait until `until` holds, for at most `minutes`, keeping how things stand in the
        timeline every minute, and saying it every ten, or when the demand changes. How long
        it took, or None."""
        start = clock.monotonic()
        next_tick = next_say = start
        demand = None
        while True:
            now = clock.monotonic()
            done = until()
            changed = self.value("demand") != demand
            if now >= next_tick or done or changed:
                line = self._tick(show)
                next_tick = now + TICK_S
                if now >= next_say or done or changed:
                    self.say(line)
                    next_say = now + SAY_S
                demand = self.value("demand")
            if done:
                return now - start
            if now - start >= minutes * 60:
                return None
            await asyncio.sleep(POLL_S)

    def _tick(self, show: Mapping[str, str]) -> str:
        """The values shown, kept in the timeline; as a line to say."""
        row: dict[str, Any] = {"t": round(clock.monotonic() - self._started)}
        parts = []
        for label, point in show.items():
            found = self.envelope(point)
            if found is None or found.value is None:
                row[label] = None
                parts.append(f"{label} -")
                continue
            row[label] = found.value
            unit = f" {_units(found.unit)}" if found.unit else ""
            quality = "" if found.quality == "good" else f" ({found.quality})"
            parts.append(f"{label} {_text(found.value)}{unit}{quality}")
        self.timeline.append(row)
        return "  " + " · ".join(parts)

    def step(
        self, what: str, outcome: str, detail: str | None = None, passed: bool | None = None
    ) -> None:
        t = round(clock.monotonic() - self._started, 1)
        self.steps.append(Step(what, outcome, detail, passed, t))
        mark = {True: "", False: "  FAILED", None: ""}[passed]
        self.say(f"{what}: {outcome}{f' ({detail})' if detail else ''}{mark}")

    async def ask(self, question: str) -> bool | None:
        answer = await self._ask(question)
        self.step(question, {True: "yes", False: "no", None: "not answered"}[answer])
        return answer

    async def judge(self, summary: str) -> None:
        """Ask the person whether the pump did what was expected, where a person must judge."""
        self.say(summary)
        self.judging = True
        self.judged = await self.ask("Did the pump do what you expected?")

    def report(self, check: str, options: Options, levers: Sequence[str]) -> Report:
        return Report(
            check=check,
            wrote=self.write,
            made=clock.now().isoformat(timespec="seconds"),
            pump=self.pump() if self._host is not None and PUMP in self._host.instances else {},
            options=asdict(options),
            steps=self.steps,
            timeline=self.timeline,
            judging=self.judging,
            judged=self.judged,
            levers=tuple(f"{profile.UNIT}/{p}" for p in levers),
        )

    def elapsed(self) -> str:
        return durations.text(clock.monotonic() - self._started)


def _text(value: Any) -> str:
    return f"{value:g}" if isinstance(value, float) else str(value)


def _units(unit: str) -> str:
    return {"degC": "°C"}.get(unit, unit)


async def terminal_ask(question: str) -> bool | None:
    """Ask on the terminal. The answer is read in a thread of its own that doesn't hold up
    the way out if the run is stopped meanwhile."""
    loop = asyncio.get_running_loop()
    answered: asyncio.Future[str | None] = loop.create_future()

    def read() -> None:
        try:
            line: str | None = input(f"{question} [y/n] ")
        except EOFError:
            line = None

        def settle() -> None:
            if not answered.done():
                answered.set_result(line)

        with contextlib.suppress(RuntimeError):  # the run ended meanwhile
            loop.call_soon_threadsafe(settle)

    threading.Thread(target=read, daemon=True).start()
    line = await answered
    if line is None:
        return None
    return line.strip().lower() in ("y", "yes")


def now_stamp() -> str:
    return datetime.now(UTC).strftime("%Y%m%d-%H%M")
