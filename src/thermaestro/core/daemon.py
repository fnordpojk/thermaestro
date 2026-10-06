"""`thermaestro run`: the long-running process.

It reads the start-up file, opens the store, runs the plugins and the web UI, and keeps
their values until it is told to stop. On SIGTERM or SIGINT it stops the plugins, writes what's
waiting, runs the shutdown hooks, and exits. The core keeps those signals for itself,
so the restore-on-exit that comes with the write path has its place here.
"""

import asyncio
import contextlib
import logging
import signal
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field

from ..cap.sockets import listen_tcp, listen_unix, unix_available
from ..files import private_directory
from ..store import Database, Layout, SecretStore, Startup, load_startup
from .audit import AuditLog
from .host import PluginHost
from .mqtt import MqttInput
from .plugins import Factory, discover
from .sensors import SensorHub
from .values import Values

log = logging.getLogger(__name__)

FLUSH_S = 60.0
PRUNE_S = 3600.0

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
    mqtt: MqttInput
    shutdown_hooks: list[ShutdownHook] = field(default_factory=list)
    """Run in order on the way out, before the plugins stop."""


async def run(
    layout: Layout,
    *,
    stop: asyncio.Event | None = None,
    factories: Mapping[str, Factory] | None = None,
    flush_s: float = FLUSH_S,
) -> None:
    """Run until `stop` is set, or until SIGTERM or SIGINT."""
    stop = stop or asyncio.Event()
    startup = load_startup(layout.startup)
    layout = startup.layout(layout)
    private_directory(layout.state)
    db = await Database.open(layout.database)
    try:
        secrets = SecretStore(layout.secrets)
        audit = AuditLog(layout.audit)
        values = Values(db)
        host = PluginHost(
            db=db,
            secrets=secrets,
            values=values,
            audit=audit,
            factories=factories if factories is not None else discover(),
        )
        sensors = SensorHub(db, values)
        mqtt = MqttInput(db, secrets, sensors)
        core = Core(layout, startup, db, secrets, audit, values, host, sensors, mqtt)
        await _serve(core, stop, flush_s)
    finally:
        await db.close()


async def _serve(core: Core, stop: asyncio.Event, flush_s: float) -> None:
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    await core.audit.record("core", "core.start")
    history = core.startup.history
    await core.sensors.load()
    sensor_tasks = [
        asyncio.create_task(core.mqtt.run()),
        asyncio.create_task(_tick(core.sensors)),
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
    stop_web = None
    try:
        from ..web.server import start as start_web

        stop_web = await start_web(core)
        log.info("Thermaestro is running")
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


async def _tick(sensors: SensorHub, every_s: float = 30.0) -> None:
    """Mark sensors that have gone quiet as stale."""
    while True:
        await asyncio.sleep(every_s)
        sensors.tick()


async def _plugin_socket(core: Core) -> asyncio.Server | None:
    path = core.startup.plugins.socket
    if path is None:
        return None
    if unix_available():
        return await listen_unix(path, core.host.attach)
    return await listen_tcp(path.with_suffix(".json"), core.host.attach)
