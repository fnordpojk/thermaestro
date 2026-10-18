"""The plugin's side of a connection.

A plugin implements `Plugin` and is run with `serve`, on an endpoint from either carrier.
`serve` answers `hello`, answers lines that aren't messages with `error`, and ends
subscriptions; the plugin answers the rest.
"""

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from functools import partial
from typing import Protocol

from .carrier import BadLine, Closed, Endpoint
from .messages import (
    PROTOCOL,
    TO_PLUGIN,
    VERSION,
    Error,
    Hello,
    Message,
    SeriesSubscribe,
    Subscribe,
    Unsubscribe,
    major,
)

log = logging.getLogger(__name__)

Send = Callable[[Message], Awaitable[None]]


class Plugin(Protocol):
    name: str
    version: str
    features: tuple[str, ...]

    async def handle(self, request: Message, send: Send) -> None:
        """Answer one request. Each runs as its own task, so a slow answer holds up
        nothing else. A subscription's runs for as long as it lasts, sending updates,
        and is canceled when it ends."""
        ...

    async def events(self, send: Send) -> None:
        """Runs while connected, for what the plugin sends unasked: health, device
        events, changed descriptions. Canceled when the connection closes. Raising ends
        the connection, so the host starts the plugin again; returning ends nothing."""
        ...


def answer_hello(hello: Hello, plugin: Plugin) -> Message:
    if hello.protocol != PROTOCOL or major(hello.version) != major(VERSION):
        return Error(
            id=hello.id,
            code="version",
            detail=f"this plugin speaks {PROTOCOL} {VERSION}",
        )
    return Hello(
        id=hello.id,
        protocol=PROTOCOL,
        version=VERSION,
        role="plugin",
        plugin=plugin.name,
        plugin_version=plugin.version,
        features=plugin.features,
    )


async def serve(endpoint: Endpoint, plugin: Plugin) -> None:
    """Run `plugin` on one connection until it closes."""
    tasks: set[asyncio.Task[None]] = set()
    subscriptions: dict[int, asyncio.Task[None]] = {}

    def start(coro: Awaitable[None]) -> asyncio.Task[None]:
        task = asyncio.ensure_future(_logged(coro))
        tasks.add(task)
        task.add_done_callback(tasks.discard)
        return task

    events = asyncio.ensure_future(plugin.events(endpoint.send))
    tasks.add(events)
    try:
        while True:
            try:
                message = await _next(endpoint, events)
            except BadLine as e:
                await endpoint.send(Error(id=e.id, code=e.code, detail=e.detail))
                continue
            if message is None:
                # Its events failed: it can't go on. Left connected it would send nothing,
                # however long; ended, the host starts it again.
                await endpoint.close()
                return
            if isinstance(message, Hello):
                await endpoint.send(answer_hello(message, plugin))
            elif isinstance(message, Unsubscribe):
                if (task := subscriptions.pop(message.id, None)) is not None:
                    task.cancel()
            elif message.type not in TO_PLUGIN:
                await endpoint.send(
                    Error(
                        id=getattr(message, "id", None),
                        code="unsupported",
                        detail=f"{message.type} is sent to the core, not to a plugin",
                    )
                )
            else:
                task = start(plugin.handle(message, endpoint.send))
                if isinstance(message, Subscribe | SeriesSubscribe):
                    subscriptions[message.id] = task
                    task.add_done_callback(partial(_forget, subscriptions, message.id))
    except Closed:
        pass
    finally:
        for task in list(tasks):
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


def _forget(
    subscriptions: dict[int, asyncio.Task[None]], id: int, task: asyncio.Task[None]
) -> None:
    if subscriptions.get(id) is task:
        del subscriptions[id]


async def _next(endpoint: Endpoint, events: "asyncio.Task[None]") -> Message | None:
    """The next message; None once the plugin's events have failed. Events that end
    without failing end nothing."""
    if events.done():
        return await endpoint.receive()
    receiving = asyncio.ensure_future(endpoint.receive())
    try:
        await asyncio.wait({receiving, events}, return_when=asyncio.FIRST_COMPLETED)
    except asyncio.CancelledError:
        receiving.cancel()
        raise
    if receiving.done():
        return receiving.result()
    receiving.cancel()
    with contextlib.suppress(asyncio.CancelledError, Closed, BadLine):
        await receiving
    error = None if events.cancelled() else events.exception()
    if error is None or isinstance(error, Closed):
        return await endpoint.receive()
    log.error("the plugin stopped; ending its connection to start it again", exc_info=error)
    return None


async def _logged(coro: Awaitable[None]) -> None:
    try:
        await coro
    except Closed:
        pass
    except Exception:
        log.exception("a plugin task failed")
