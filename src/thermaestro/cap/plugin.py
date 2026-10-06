"""The plugin's side of a connection.

A plugin implements `Plugin` and is run with `serve`, on an endpoint from either carrier.
`serve` answers `hello`, answers lines that aren't messages with `error`, and ends
subscriptions; the plugin answers the rest.
"""

import asyncio
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
        and is cancelled when it ends."""
        ...

    async def events(self, send: Send) -> None:
        """Runs while connected, for what the plugin sends unasked: health, device
        events, changed descriptions. Cancelled when the connection closes."""
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

    start(plugin.events(endpoint.send))
    try:
        while True:
            try:
                message = await endpoint.receive()
            except BadLine as e:
                await endpoint.send(Error(id=e.id, code=e.code, detail=e.detail))
                continue
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


async def _logged(coro: Awaitable[None]) -> None:
    try:
        await coro
    except Closed:
        pass
    except Exception:
        log.exception("a plugin task failed")
