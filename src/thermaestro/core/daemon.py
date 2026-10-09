"""`thermaestro run`: the long-running process.

It reads the start-up file, opens the store, runs the plugins and the web UI, and keeps
their values until it is told to stop. On SIGTERM or SIGINT it puts every lever back as it
was found, runs the other shutdown hooks, stops the plugins, writes what's waiting, and
exits. The core keeps those signals for itself, so the restore always runs first.
"""

import asyncio
import contextlib
import logging
import signal
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from ..cap.sockets import listen_tcp, listen_unix, unix_available
from ..files import private_directory
from ..intents import Intents
from ..store import Database, Layout, SecretStore, Startup, load_startup
from .audit import AuditLog
from .discovery import Publisher
from .executor import Executor
from .host import PluginHost
from .house import House
from .mqtt import MqttClient
from .plugins import Factory, discover
from .sensors import SensorHub
from .series import Series
from .values import Values
from .weather import Weather

if TYPE_CHECKING:
    from ..planner import Planner

log = logging.getLogger(__name__)

FLUSH_S = 60.0
PRUNE_S = 3600.0
INTENTS_S = 60.0
"""How often intents move along with time, and the house's capabilities are read again."""
SEED_EVERY = 10
"""Seeding is tried every this many rounds, until every part of the house has been."""

ShutdownHook = Callable[[], Awaitable[None]]


@dataclass
class Core:
    """What a running Thermaestro holds; the web UI and later stages work with it."""

    layout: Layout
    startup: Startup
    db: Database
    secrets: SecretStore
    audit: AuditLog
    values: Values
    host: PluginHost
    sensors: SensorHub
    mqtt: MqttClient
    weather: Weather
    discovery: Publisher
    executor: Executor
    house: House
    intents: Intents
    planner: "Planner"
    shutdown_hooks: list[ShutdownHook] = field(default_factory=list)
    """Run in order on the way out, before the plugins stop."""


async def run(
    layout: Layout,
    *,
    stop: asyncio.Event | None = None,
    factories: Mapping[str, Factory] | None = None,
    flush_s: float = FLUSH_S,
    started: Callable[[Core], Awaitable[None]] | None = None,
) -> None:
    """Run until `stop` is set, or until SIGTERM or SIGINT. `started` is called once
    everything runs."""
    stop = stop or asyncio.Event()
    startup = load_startup(layout.startup)
    layout = startup.layout(layout)
    private_directory(layout.state)
    db = await Database.open(layout.database)
    try:
        secrets = SecretStore(layout.secrets)
        audit = AuditLog(layout.audit)
        values = Values(db)
        series = Series(db)
        host = PluginHost(
            db=db,
            secrets=secrets,
            values=values,
            audit=audit,
            series=series,
            factories=factories if factories is not None else discover(),
        )
        sensors = SensorHub(db, values)
        mqtt = MqttClient(db, secrets, sensors)
        weather = Weather(db, series, values, host)
        discovery = Publisher(db, values, host, sensors, series, mqtt)
        executor = Executor(db, host, values, audit)
        house = House(db, host, values)
        intents = Intents(db, audit, capabilities=lambda: house.capabilities)
        from ..planner import Planner  # the planner is built on the core's parts

        planner = Planner(
            db, values, host, executor, intents, house, audit, sensors=sensors, series=series
        )
        core = Core(
            layout,
            startup,
            db,
            secrets,
            audit,
            values,
            host,
            sensors,
            mqtt,
            weather,
            discovery,
            executor,
            house,
            intents,
            planner,
        )
        # The planner stops first, so nothing asks for a change once levers are put back.
        core.shutdown_hooks.append(planner.stop)
        core.shutdown_hooks.append(_restore(executor))
        await _serve(core, stop, flush_s, started)
    finally:
        await db.close()


async def _serve(
    core: Core,
    stop: asyncio.Event,
    flush_s: float,
    started: Callable[[Core], Awaitable[None]] | None,
) -> None:
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    await core.audit.record("core", "core.start")
    history = core.startup.history
    await core.sensors.load()
    sensor_tasks = [
        asyncio.create_task(core.mqtt.run()),
        asyncio.create_task(_tick(core.sensors)),
        asyncio.create_task(core.weather.run()),
        asyncio.create_task(_intents(core)),
    ]
    writer = asyncio.create_task(
        core.values.run(
            flush_s=flush_s,
            prune_s=PRUNE_S,
            raw_days=history.raw_days,
            aggregate_days=history.aggregate_days,
        )
    )
    server = await _plugin_socket(core)
    await core.host.start()
    await core.executor.start()
    core.planner.start()
    stop_web = None
    try:
        from ..web.server import start as start_web

        stop_web = await start_web(core)
        log.info("Thermaestro is running")
        if started is not None:
            await started(core)
        await stop.wait()
    finally:
        log.info("stopping")
        if stop_web is not None:
            await stop_web()  # first, so nothing is changed on the way out
        for hook in core.shutdown_hooks:
            try:
                await hook()
            except Exception:
                log.exception("a shutdown step failed")
        await core.executor.stop()
        await core.host.stop()
        if server is not None:
            server.close()
            server.close_clients()
            await server.wait_closed()
        for task in sensor_tasks:
            task.cancel()
        await asyncio.gather(*sensor_tasks, return_exceptions=True)
        writer.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await writer
        await core.audit.record("core", "core.stop")
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.remove_signal_handler(sig)


def _restore(executor: Executor) -> ShutdownHook:
    async def hook() -> None:
        await executor.restore("Thermaestro is stopping")

    return hook


async def _intents(core: Core, every_s: float = INTENTS_S) -> None:
    """Keep intents moving with time and the house's capabilities current; seed defaults
    from how the house runs, part by part, as soon as each has a day of readings."""
    rounds = 0
    while True:
        await asyncio.sleep(every_s)
        try:
            await core.house.refresh()
            await core.intents.advance()
            if rounds % SEED_EVERY == 0:
                await core.intents.seed(await core.house.found())
        except Exception:
            log.exception("keeping intents failed")
        rounds += 1


async def _tick(sensors: SensorHub, every_s: float = 30.0) -> None:
    """Mark sensors that have gone quiet as stale, and keep how often each reports."""
    while True:
        await asyncio.sleep(every_s)
        sensors.tick()
        try:
            await sensors.save()
        except Exception:
            log.exception("keeping the sensors' rhythms failed; trying again later")


async def _plugin_socket(core: Core) -> asyncio.Server | None:
    path = core.startup.plugins.socket
    if path is None:
        return None
    if unix_available():
        return await listen_unix(path, core.host.attach)
    return await listen_tcp(path.with_suffix(".json"), core.host.attach)
