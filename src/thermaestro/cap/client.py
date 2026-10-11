"""The core's side of a connection to a plugin.

`Link` sends requests with fresh ids and routes what comes back: a message with a request's
id goes to that request, and anything else (health, device events, changed descriptions)
to the event callback.
"""

import asyncio
import contextlib
import itertools
import logging
from collections.abc import AsyncIterator, Callable, Collection, Mapping
from datetime import datetime
from types import TracebackType
from typing import Self

from .carrier import BadLine, Closed, Endpoint
from .messages import (
    FINAL_STAGES,
    PROTOCOL,
    VERSION,
    Act,
    Describe,
    Described,
    Error,
    Fate,
    Hello,
    Message,
    Op,
    Read,
    SeriesData,
    SeriesGet,
    SeriesSubscribe,
    SeriesUpdate,
    Subscribe,
    Unsubscribe,
    Update,
    Values,
    Write,
)
from .model import Value

log = logging.getLogger(__name__)

TIMEOUT_S = 10.0


class CapError(Exception):
    """The plugin answered with `error`."""

    def __init__(self, error: Error) -> None:
        super().__init__(f"{error.code}: {error.detail}")
        self.code = error.code
        self.detail = error.detail


class UnexpectedReply(Exception):
    """The plugin answered a request with a message that doesn't answer it."""

    def __init__(self, request: str, reply: Message) -> None:
        super().__init__(f"{request} answered with {reply.type}")
        self.reply = reply


_END = object()


class Link:
    def __init__(
        self, endpoint: Endpoint, on_event: Callable[[Message], None] | None = None
    ) -> None:
        self._endpoint = endpoint
        self._on_event = on_event
        self._ids = itertools.count(1)
        self._waiting: dict[int, asyncio.Queue[object]] = {}
        self._task: asyncio.Task[None] | None = None
        self.bad_lines: list[BadLine] = []
        """Lines from the plugin that weren't messages."""
        self.done = asyncio.Event()
        """Set when the connection has ended."""

    def start(self) -> None:
        self._task = asyncio.create_task(self._receive())

    async def close(self) -> None:
        await self._endpoint.close()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task

    async def __aenter__(self) -> Self:
        self.start()
        return self

    async def __aexit__(
        self, t: type[BaseException] | None, e: BaseException | None, tb: TracebackType | None
    ) -> None:
        await self.close()

    def next_id(self) -> int:
        return next(self._ids)

    async def hello(
        self, *, version: str = VERSION, features: Collection[str] = (), timeout: float = TIMEOUT_S
    ) -> Hello:
        reply = await self.request(
            Hello(
                id=self.next_id(),
                protocol=PROTOCOL,
                version=version,
                role="core",
                features=tuple(features),
            ),
            timeout,
        )
        return _expect(Hello, "hello", reply)

    async def describe(self, *, timeout: float = TIMEOUT_S) -> Described:
        reply = await self.request(Describe(id=self.next_id()), timeout)
        return _expect(Described, "describe", reply)

    async def read(
        self,
        points: Collection[str],
        *,
        after: datetime | None = None,
        timeout: float = TIMEOUT_S,
    ) -> Values:
        reply = await self.request(
            Read(id=self.next_id(), points=tuple(points), after=after), timeout
        )
        return _expect(Values, "read", reply)

    async def act(
        self,
        lever: str,
        op: Op,
        params: Mapping[str, Value] | None = None,
        *,
        timeout: float = TIMEOUT_S,
    ) -> AsyncIterator[Fate]:
        """Each fate the plugin reports, up to and including a final one. `timeout` is
        the longest wait for the next."""
        message = Act(id=self.next_id(), lever=lever, op=op, params=dict(params or {}))
        async for fate in self._fates(message, "act", timeout):
            yield fate

    async def write(
        self, point: str, value: float, *, timeout: float = TIMEOUT_S
    ) -> AsyncIterator[Fate]:
        """A person's change of one of the device's own settings: each fate, as `act`."""
        message = Write(id=self.next_id(), point=point, value=value)
        async for fate in self._fates(message, "write", timeout):
            yield fate

    async def _fates(self, message: Act | Write, name: str, timeout: float) -> AsyncIterator[Fate]:
        async with self._exchange(message) as replies:
            while True:
                fate = _expect(Fate, name, await self._next(replies, timeout))
                yield fate
                if fate.stage in FINAL_STAGES:
                    return

    def subscribe(
        self, points: Collection[str], *, min_interval_s: float = 0.0, on_change: bool = True
    ) -> "Subscription":
        message = Subscribe(
            id=self.next_id(),
            points=tuple(points),
            min_interval_s=min_interval_s,
            on_change=on_change,
        )
        return Subscription(self, message)

    async def series_get(
        self, series: str, start: datetime, end: datetime, *, timeout: float = TIMEOUT_S
    ) -> SeriesData:
        reply = await self.request(
            SeriesGet(id=self.next_id(), series=series, start=start, end=end), timeout
        )
        return _expect(SeriesData, "series.get", reply)

    def series_subscribe(self, series: str) -> "SeriesSubscription":
        return SeriesSubscription(self, SeriesSubscribe(id=self.next_id(), series=series))

    async def request(self, message: Message, timeout: float = TIMEOUT_S) -> Message:
        """Send a request and return the first message with its id."""
        async with self._exchange(message) as replies:
            return await self._next(replies, timeout)

    @contextlib.asynccontextmanager
    async def _exchange(self, message: Message) -> AsyncIterator[asyncio.Queue[object]]:
        id = getattr(message, "id", None)
        if not isinstance(id, int):
            raise ValueError(f"{message.type} isn't a request")
        replies: asyncio.Queue[object] = asyncio.Queue()
        self._waiting[id] = replies
        try:
            await self._endpoint.send(message)
            yield replies
        finally:
            self._waiting.pop(id, None)

    @staticmethod
    async def _next(replies: asyncio.Queue[object], timeout: float) -> Message:
        reply = await asyncio.wait_for(replies.get(), timeout)
        if reply is _END:
            raise Closed
        if isinstance(reply, Error):
            raise CapError(reply)
        return reply  # type: ignore[return-value]

    async def _receive(self) -> None:
        try:
            while True:
                try:
                    message = await self._endpoint.receive()
                except BadLine as e:
                    log.warning("a plugin sent a line that isn't a message: %s", e.detail)
                    self.bad_lines.append(e)
                    continue
                id = getattr(message, "id", None)
                replies = self._waiting.get(id) if isinstance(id, int) else None
                if replies is not None:
                    replies.put_nowait(message)
                elif self._on_event is not None:
                    self._on_event(message)
        except Closed:
            pass
        finally:
            for replies in self._waiting.values():
                replies.put_nowait(_END)
            self.done.set()


class Subscription:
    """Updates for a subscription, as an async iterator; leaving its `async with`
    unsubscribes."""

    def __init__(self, link: Link, message: Subscribe) -> None:
        self._link = link
        self.message = message
        self._context: contextlib.AbstractAsyncContextManager[asyncio.Queue[object]] | None = None
        self._replies: asyncio.Queue[object] | None = None

    async def __aenter__(self) -> Self:
        self._context = self._link._exchange(self.message)
        self._replies = await self._context.__aenter__()
        return self

    async def __aexit__(
        self, t: type[BaseException] | None, e: BaseException | None, tb: TracebackType | None
    ) -> None:
        with contextlib.suppress(Closed):
            await self._link._endpoint.send(Unsubscribe(id=self.message.id))
        if self._context is not None:
            await self._context.__aexit__(t, e, tb)

    async def next(self, timeout: float = TIMEOUT_S) -> Update:
        if self._replies is None:
            raise RuntimeError("use the subscription in `async with`")
        reply = await Link._next(self._replies, timeout)
        return _expect(Update, "subscribe", reply)


class SeriesSubscription:
    """New and revised intervals of one series, as published; leaving its `async with`
    unsubscribes."""

    def __init__(self, link: Link, message: SeriesSubscribe) -> None:
        self._link = link
        self.message = message
        self._context: contextlib.AbstractAsyncContextManager[asyncio.Queue[object]] | None = None
        self._replies: asyncio.Queue[object] | None = None

    async def __aenter__(self) -> Self:
        self._context = self._link._exchange(self.message)
        self._replies = await self._context.__aenter__()
        return self

    async def __aexit__(
        self, t: type[BaseException] | None, e: BaseException | None, tb: TracebackType | None
    ) -> None:
        with contextlib.suppress(Closed):
            await self._link._endpoint.send(Unsubscribe(id=self.message.id))
        if self._context is not None:
            await self._context.__aexit__(t, e, tb)

    async def next(self, timeout: float = TIMEOUT_S) -> SeriesUpdate:
        if self._replies is None:
            raise RuntimeError("use the subscription in `async with`")
        reply = await Link._next(self._replies, timeout)
        return _expect(SeriesUpdate, "series.subscribe", reply)


def _expect[M](kind: type[M], request: str, reply: Message) -> M:
    if not isinstance(reply, kind):
        raise UnexpectedReply(request, reply)
    return reply
