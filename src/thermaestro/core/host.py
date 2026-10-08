"""Runs the plugin instances the settings name, and keeps what they describe and send.

Each instance is supervised: if its plugin fails or its connection ends, the instance is
restarted after a growing pause, and the failure is reported. One plugin failing never
stops the others or the core.

Nothing here acts on a lever. Writing to devices comes with the write path.
"""

import asyncio
import contextlib
import logging
import time
from collections import deque
from collections.abc import Coroutine, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from ..cap import Closed, Endpoint, Link, Message, pair, serve
from ..cap.messages import FEATURES, Described, DeviceEvent, ForeignWrite, Health, Hello
from ..store import Database, Plugin, SecretStore
from .audit import AuditLog
from .plugins import Factory, PluginContext, PluginStore
from .series import Series
from .values import Values

log = logging.getLogger(__name__)

STABLE_S = 60.0
"""A session that lasted this long resets the failure count."""


class State(StrEnum):
    STARTING = "starting"
    UP = "up"
    RESTARTING = "restarting"
    WAITING = "waiting"
    """An out-of-process plugin that hasn't connected."""
    STOPPED = "stopped"


@dataclass
class Instance:
    id: str
    setting: Plugin
    state: State = State.STARTING
    hello: Hello | None = None
    described: Described | None = None
    health: Health | None = None
    failures: int = 0
    last_error: str | None = None
    events: deque[Message] = field(default_factory=lambda: deque(maxlen=50))
    """Device events and foreign writes, newest last."""


class PluginHost:
    def __init__(
        self,
        *,
        db: Database,
        secrets: SecretStore,
        values: Values,
        audit: AuditLog,
        factories: Mapping[str, Factory],
        series: Series | None = None,
        backoff_s: tuple[float, float] = (1.0, 60.0),
        timeout_s: float = 10.0,
        describe_timeout_s: float = 60.0,
    ) -> None:
        self._db = db
        self._secrets = secrets
        self.values = values
        self.series = series
        self._audit = audit
        self._factories = factories
        self._backoff = backoff_s
        self._timeout = timeout_s
        self._describe_timeout = describe_timeout_s
        """A plugin may first have to identify its device: a Nibe pump names its model
        every 15 s."""
        self.instances: dict[str, Instance] = {}
        self._tasks: set[asyncio.Task[None]] = set()
        self._supervisors: dict[str, asyncio.Task[None]] = {}

    async def start(self) -> None:
        for id, setting in (await self._db.all(Plugin)).items():
            self._begin(id, setting)

    async def apply(self, id: str) -> None:
        """Start, restart or stop one instance after its setting changed."""
        supervisor = self._supervisors.pop(id, None)
        if supervisor is not None:
            supervisor.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await supervisor
        self.instances.pop(id, None)
        setting = await self._db.get(Plugin, id)
        if setting is not None:
            self._begin(id, setting)

    def _begin(self, id: str, setting: Plugin) -> None:
        if not setting.enabled:
            return
        instance = Instance(id, setting)
        self.instances[id] = instance
        if setting.plugin in self._factories:
            self._supervisors[id] = self._spawn(self._supervise(instance))
        else:
            instance.state = State.WAITING

    async def stop(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        for instance in self.instances.values():
            instance.state = State.STOPPED

    async def attach(self, endpoint: Endpoint) -> None:
        """Run an out-of-process plugin's connection, for as long as it lasts. It is
        matched to a waiting instance by the plugin name in its `hello`."""
        matched: list[Instance] = []

        def on_event(message: Message) -> None:
            if matched:
                self._event(matched[0], message)

        link = Link(endpoint, on_event=on_event)
        link.start()
        try:
            hello = await link.hello(timeout=self._timeout, features=FEATURES)
            instance = next(
                (
                    i
                    for i in self.instances.values()
                    if i.state == State.WAITING and i.setting.plugin == hello.plugin
                ),
                None,
            )
            if instance is None:
                log.warning("plugin %s connected, but no instance is waiting for it", hello.plugin)
                return
            matched.append(instance)
            instance.hello = hello
            try:
                await self._session(instance, link)
            except (Closed, TimeoutError) as e:
                instance.last_error = type(e).__name__
            finally:
                instance.state = State.WAITING
        finally:
            await link.close()

    def _spawn(self, coro: Coroutine[Any, Any, None]) -> asyncio.Task[None]:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def _supervise(self, instance: Instance) -> None:
        factory = self._factories[instance.setting.plugin]
        while True:
            started = time.monotonic()
            try:
                await self._run_in_process(instance, factory)
                error = "the plugin ended its connection"
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.exception("plugin instance %s failed", instance.id)
                error = f"{type(e).__name__}: {e}"
            if time.monotonic() - started >= STABLE_S:
                instance.failures = 0
            instance.failures += 1
            instance.last_error = error
            instance.state = State.RESTARTING
            pause = min(self._backoff[1], self._backoff[0] * 2 ** (instance.failures - 1))
            log.warning(
                "plugin instance %s stopped (%s); restarting in %.0f s", instance.id, error, pause
            )
            await self._audit.record(
                f"plugin:{instance.setting.plugin}",
                "plugin.restart",
                outcome="failed",
                details={"instance": instance.id, "error": error},
            )
            await asyncio.sleep(pause)

    async def _run_in_process(self, instance: Instance, factory: Factory) -> None:
        context = PluginContext(
            instance.id,
            instance.setting.settings,
            self._secrets,
            PluginStore(self._db, instance.id),
        )
        plugin = factory(context)
        core, plugin_side = pair()
        served = asyncio.create_task(serve(plugin_side, plugin))
        link = Link(core, on_event=lambda m: self._event(instance, m))
        link.start()
        try:
            instance.hello = await link.hello(timeout=self._timeout, features=FEATURES)
            await self._session(instance, link)
        except Closed:
            pass
        finally:
            await link.close()
            await plugin_side.close()
            served.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await served

    async def _session(self, instance: Instance, link: Link) -> None:
        """Describe, subscribe to every point, and keep the values until the connection
        ends (Closed)."""
        instance.described = await link.describe(timeout=self._describe_timeout)
        instance.state = State.UP
        await self._audit.record(
            f"plugin:{instance.setting.plugin}",
            "plugin.start",
            details={
                "instance": instance.id,
                "version": instance.hello.plugin_version if instance.hello else None,
            },
        )
        followed = None
        if self.series is not None and instance.described.series:
            followed = asyncio.create_task(
                self.series.follow(instance.id, link, instance.described.series)
            )
        try:
            await self._points(instance, link)
        finally:
            if followed is not None:
                followed.cancel()
                with contextlib.suppress(asyncio.CancelledError, Closed):
                    await followed

    async def _points(self, instance: Instance, link: Link) -> None:
        assert instance.described is not None  # noqa: S101 - described before this
        points = [p.path for p in instance.described.points]
        if not points:
            await link.done.wait()
            raise Closed
        async with link.subscribe(points) as subscription:
            while True:
                try:
                    update = await subscription.next(timeout=3600.0)
                except TimeoutError:
                    continue  # no values for an hour; health says why, if anything
                described = {p.path: p for p in instance.described.points}
                for envelope in update.values:
                    self.values.add(instance.id, envelope, described.get(envelope.point))

    def _event(self, instance: Instance, message: Message) -> None:
        if isinstance(message, Health):
            instance.health = message
        elif isinstance(message, DeviceEvent | ForeignWrite):
            instance.events.append(message)
        elif isinstance(message, Described) and instance.described is not None:
            instance.described = _merge(instance.described, message)


def _merge(old: Described, change: Described) -> Described:
    """A partial `described` applied to the full one: changed items replace, removed
    paths go."""
    removed = set(change.removed)

    def merged[T](current: tuple[T, ...], new: tuple[T, ...], key: str) -> tuple[T, ...]:
        replaced = {getattr(item, key) for item in new}
        kept = [i for i in current if getattr(i, key) not in replaced | removed]
        return (*kept, *new)

    return old.model_copy(
        update={
            "nodes": merged(old.nodes, change.nodes, "path"),
            "points": merged(old.points, change.points, "path"),
            "levers": merged(old.levers, change.levers, "path"),
            "series": merged(old.series, change.series, "id"),
        }
    )
