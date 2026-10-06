"""How messages travel: in process as objects, or over a socket as JSON lines.

Both carriers offer the same endpoint, so a plugin written against it runs in process or
out of process unchanged.
"""

import asyncio
import contextlib
import json
import logging
from typing import Literal, Protocol

from pydantic import ValidationError

from .messages import MESSAGES, TYPES, Message

log = logging.getLogger(__name__)

LINE_LIMIT = 1 << 20
"""The longest line a socket endpoint takes, in bytes."""


class Closed(Exception):
    """The connection is closed."""


class BadLine(Exception):
    """A line that isn't a message. The connection goes on."""

    def __init__(
        self, code: Literal["unsupported", "invalid"], detail: str, id: int | None
    ) -> None:
        super().__init__(detail)
        self.code: Literal["unsupported", "invalid"] = code
        self.detail = detail
        self.id = id


class Endpoint(Protocol):
    """One side of a connection."""

    async def send(self, message: Message) -> None:
        """Raises Closed."""
        ...

    async def receive(self) -> Message:
        """The next message. Raises Closed, and BadLine for a line that isn't one."""
        ...

    async def close(self) -> None: ...


_CLOSED = object()


class _QueueEndpoint:
    def __init__(self, inbox: asyncio.Queue[object], outbox: asyncio.Queue[object]) -> None:
        self._inbox = inbox
        self._outbox = outbox
        self._closed = False

    async def send(self, message: Message) -> None:
        if self._closed:
            raise Closed
        self._outbox.put_nowait(message)

    async def receive(self) -> Message:
        item = await self._inbox.get()
        if item is _CLOSED:
            self._inbox.put_nowait(_CLOSED)  # every later receive ends the same way
            self._closed = True
            raise Closed
        return item  # type: ignore[return-value]

    async def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._outbox.put_nowait(_CLOSED)
            self._inbox.put_nowait(_CLOSED)


def pair() -> tuple[Endpoint, Endpoint]:
    """Two connected endpoints in this process: the core's and the plugin's."""
    a: asyncio.Queue[object] = asyncio.Queue()
    b: asyncio.Queue[object] = asyncio.Queue()
    return _QueueEndpoint(a, b), _QueueEndpoint(b, a)


class StreamEndpoint:
    """JSON lines over a stream: one message per line, UTF-8."""

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._reader = reader
        self._writer = writer

    async def send(self, message: Message) -> None:
        if self._writer.is_closing():
            raise Closed
        self._writer.write(MESSAGES.dump_json(message) + b"\n")
        try:
            await self._writer.drain()
        except (ConnectionError, RuntimeError) as e:
            raise Closed from e

    async def receive(self) -> Message:
        try:
            line = await self._reader.readline()
        except (asyncio.LimitOverrunError, ValueError) as e:
            await self.close()  # the stream can't find the next line
            raise Closed from e
        except ConnectionError as e:
            raise Closed from e
        if not line:
            raise Closed
        return parse(line)

    async def close(self) -> None:
        self._writer.close()
        with contextlib.suppress(ConnectionError):
            await self._writer.wait_closed()


def parse(line: bytes) -> Message:
    """One line as a message. Raises BadLine, with the request's id where there is one."""
    try:
        data = json.loads(line)
    except ValueError as e:
        raise BadLine("invalid", f"not JSON: {e}", None) from e
    if not isinstance(data, dict):
        raise BadLine("invalid", "not a JSON object", None)
    id = data.get("id")
    id = id if isinstance(id, int) and not isinstance(id, bool) and id >= 0 else None
    kind = data.get("type")
    if kind not in TYPES:
        raise BadLine("unsupported", f"unknown message type {kind!r}", id)
    try:
        return MESSAGES.validate_python(data)
    except ValidationError as e:
        problems = "; ".join(
            f"{'.'.join(str(p) for p in err['loc'][1:])}: {err['msg']}" for err in e.errors()
        )
        raise BadLine("invalid", problems, id) from e
