"""The core against the plant, in simulated time: the Nibe plugin over the in-process bus,
the plant's sensors, the plugin host, the values and the executor, as the daemon runs
them. Run inside `vloop.simulate`.

A `decide` callback stands in for the planner: it is called every slot (15 minutes) and
asks for changes through `Sim.act`, which records each request with its slot. A control
run records the plant's state at every step, so a shadow run can be given the same
inputs.
"""

import asyncio
import contextlib
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from thermaestro import clock
from thermaestro.cap.messages import Op
from thermaestro.cap.model import Value
from thermaestro.core import AuditLog, Executor, Key, PluginHost, State, Values
from thermaestro.core.executor import COUNTED, Result
from thermaestro.nibe import profile
from thermaestro.nibe.plugin import NibePlugin
from thermaestro.store import Control, Database, LeverMode, NibeGateway, Plugin, SecretStore

from .bus import SimBus
from .model import STEP_S, Plant, begin
from .scenario import Scenario
from .sensors import PlantSensors
from .vloop import every, sleep_until

SLOT_S = 900.0
PUMP = "pump"
POLL_ROUND_S = 300.0
"""The plugin's polling, spaced: values that change slowly are read every 5 minutes."""


@dataclass(frozen=True)
class Frame:
    """The plant as the pump and the sensors showed it, at one step."""

    t: float
    registers: dict[int, int]
    registers32: dict[int, int]
    indoor: tuple[float, ...]
    house_kw: float


@dataclass(frozen=True)
class Request:
    """What the stand-in planner asked for, in which slot, and what came of it."""

    slot: float
    lever: str
    op: Op
    params: dict[str, Value]
    outcome: str
    detail: str | None = None

    @property
    def intended(self) -> bool:
        """Sent, or in shadow, would have been."""
        return self.outcome in COUNTED


class Replay:
    """The plant as a control run recorded it, step by step: inputs for a shadow run."""

    def __init__(self, frames: list[Frame]) -> None:
        self._frames = frames
        self._i = 0
        self.t = frames[0].t
        self.registers = dict(frames[0].registers)
        self.registers32 = dict(frames[0].registers32)
        self.writes: list[tuple[int, int]] = []

    @property
    def _frame(self) -> Frame:
        return self._frames[self._i]

    def run(self, seconds: float) -> None:
        end = self.t + seconds
        while self._i + 1 < len(self._frames) and self._frames[self._i + 1].t <= end + 1e-9:
            self._i += 1
        self.t = end
        self.registers.clear()
        self.registers.update(self._frame.registers)
        self.registers32.clear()
        self.registers32.update(self._frame.registers32)

    def write(self, register: int, value: int) -> bool:
        self.writes.append((register, value))
        return True

    def indoor(self, number: int = 1) -> float:
        return self._frame.indoor[number - 1]

    @property
    def house_kw(self) -> float:
        return self._frame.house_kw

    @property
    def systems(self) -> int:
        return len(self._frame.indoor)


@dataclass
class Sim:
    scenario: Scenario
    path: Path
    levers: dict[str, LeverMode] = field(default_factory=dict)
    """By path below the pump's unit: `dhw/block`."""
    confirmed_off: dict[str, tuple[str, ...]] | None = None
    """The competing features the household confirmed off, by lever; by default all of
    them, for every lever."""
    replay: list[Frame] | None = None
    record: bool = False

    plant: Plant | Replay = field(init=False)
    bus: SimBus = field(init=False)
    frames: list[Frame] = field(default_factory=list)
    requests: list[Request] = field(default_factory=list)
    trace: list[dict[str, Any]] = field(default_factory=list)
    db: Database = field(init=False)
    values: Values = field(init=False)
    host: PluginHost = field(init=False)
    executor: Executor = field(init=False)
    _tasks: list[asyncio.Task[None]] = field(default_factory=list)

    # --- running ---------------------------------------------------------------------------

    async def start(self) -> None:
        if not hasattr(self, "plant"):
            self.plant = begin(self.scenario) if self.replay is None else Replay(self.replay)
            self.bus = SimBus(self.plant)
            self.db = await Database.open(self.path / "sim.db")
            await self.db.put(
                Plugin(plugin="nibe", settings={"host": "plant.invalid", "model": "F1245"}), PUMP
            )
            await self.db.put(Plugin(plugin="plant_sensors"), "sensors")
            confirmed = self.confirmed_off
            if confirmed is None:  # the pump's own schedule and price adaption are off
                everything = (profile.SCHEDULE.name, profile.PRICE_ADAPTION.name)
                confirmed = dict.fromkeys(self.levers, everything)
            await self.db.put(
                Control(
                    levers={f"{PUMP}:hp1/{k}": v for k, v in self.levers.items()},
                    confirmed_off={f"{PUMP}:hp1/{k}": tuple(v) for k, v in confirmed.items()},
                )
            )
            self._tasks.append(asyncio.create_task(every(STEP_S, self._step)))
        audit = AuditLog(self.path / "audit")
        self.values = Values(self.db)
        bus, plant = self.bus, self.plant

        def nibe(context: Any) -> NibePlugin:
            return NibePlugin(
                NibeGateway.model_validate(dict(context.settings)),
                connect_fn=bus.connect,
                state=context.state,
                poll_round_s=POLL_ROUND_S,
                health_interval_s=60.0,
                identify_timeout_s=120.0,
            )

        self.host = PluginHost(
            db=self.db,
            secrets=SecretStore(self.path / "secrets.json"),
            values=self.values,
            audit=audit,
            factories={"nibe": nibe, "plant_sensors": lambda c: PlantSensors(plant)},
        )
        self.executor = Executor(self.db, self.host, self.values, audit)
        await self.host.start()
        async with asyncio.timeout(900):
            while not all(i.state is State.UP for i in self.host.instances.values()):
                await asyncio.sleep(1.0)
            while not all(self.good(p) for p in self._needed()):
                await asyncio.sleep(1.0)
        await self.executor.start()

    def _needed(self) -> set[str]:
        """The points the levers read back and rest on, below the unit."""
        described = self.host.instances[PUMP].described
        assert described is not None
        points = set()
        for lever in described.levers:
            if lever.verify.kind == "readback" and lever.verify.point:
                points.add(lever.verify.point)
            points.update(c.split()[0] for c in lever.preconditions.value or ())
        return {p.removeprefix("hp1/") for p in points}

    async def stop(self, *, restore: bool = True) -> None:
        """Stop the core: with `restore`, as the daemon does on its way out; without, as
        if the process died."""
        if restore:
            await self.executor.restore("Thermaestro is stopping")
        await self.executor.stop()
        await self.host.stop()
        await self.bus.close()

    async def close(self) -> None:
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        await self.db.close()

    async def run(
        self, days: float, decide: Callable[["Sim"], Awaitable[None]] | None = None
    ) -> None:
        """Run for `days`, calling `decide` at every slot, the trace kept every slot. While
        it runs, the stand-in planner checks in every minute, as a live planner does."""
        tasks = [asyncio.create_task(every(SLOT_S, self._snapshot))]
        if decide is not None:
            self.executor.heartbeat()
            tasks.append(asyncio.create_task(every(60.0, self.executor.heartbeat)))
            tasks.append(asyncio.create_task(every(SLOT_S, lambda: decide(self))))
        try:
            await asyncio.sleep(days * 86_400)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def advance(self, seconds: float) -> None:
        await asyncio.sleep(seconds)

    async def until(self, t: float) -> None:
        await sleep_until(t)

    # --- asking ----------------------------------------------------------------------------

    async def act(
        self, lever: str, op: Op, params: dict[str, Value] | None = None, *, who: str = "planner"
    ) -> Result:
        slot = clock.time() // SLOT_S * SLOT_S
        result = await self.executor.act(
            f"{PUMP}:hp1/{lever}", op, dict(params or {}), who=who, why="the stand-in planner"
        )
        self.requests.append(
            Request(slot, lever, op, dict(params or {}), result.outcome, result.detail)
        )
        return result

    def value(self, point: str, instance: str = PUMP) -> Any:
        """A point's latest value, if good; None otherwise."""
        prefix = "hp1/" if instance == PUMP else ""
        found = self.values.latest.get(Key(instance, f"{prefix}{point}"))
        return found.value if found is not None and found.quality == "good" else None

    def good(self, point: str) -> bool:
        return self.value(point) is not None

    # --- the plant -------------------------------------------------------------------------

    def _step(self) -> None:
        self.plant.run(clock.time() - self.plant.t)
        if self.record and isinstance(self.plant, Plant):
            p = self.plant
            self.frames.append(
                Frame(
                    p.t,
                    dict(p.registers),
                    dict(p.registers32),
                    tuple(z.air for z in p.zones),
                    p.house_kw,
                )
            )

    def _snapshot(self) -> None:
        p = self.plant
        row: dict[str, Any] = {"t": clock.time()}
        if isinstance(p, Plant):
            row.update(
                indoor=[round(z.air, 2) for z in p.zones],
                top=round(p.top, 2),
                bottom=round(p.bottom, 2),
                demand=p.demand,
                compressor=p.compressor_on,
                dm=round(p.dm),
                house_kw=round(p.house_kw, 3),
                addition_kw=p.addition_kw,
            )
        self.trace.append(row)


@contextlib.asynccontextmanager
async def running(sim: Sim) -> AsyncIterator[Sim]:
    """The sim started, then stopped as the daemon would and closed."""
    await sim.start()
    try:
        yield sim
    finally:
        with contextlib.suppress(Exception):
            await sim.stop()
        await sim.close()
