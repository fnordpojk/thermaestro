"""Errors that would otherwise reach only the log, as events.

A part that fails (learning a tank, a planning round, a page) is raised as an event,
`failure:<part>`, saying what failed, the error and an ID. The same ID is in the log line
with the traceback, and on a page that couldn't be shown, so the three can be matched up.
The event is for those who may see the house's workings, and reaches whoever turned on
notifications about the house. It clears when that part next works. A failure with no
next time to tell it worked by (a step of a shutdown) is a moment instead: raised and
cleared at once, at most once an hour for the same part.

The ID is the same for the same fault: the error's type and where it was raised.

`failures` is the one the daemon attaches to its events. Before that (the command line,
most tests), a failure is only logged.
"""

import asyncio
import hashlib
import logging
import traceback
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

from .. import clock
from .events import Events, Raise, mark

log = logging.getLogger(__name__)

PREFIX = "failure:"
FAILED = mark("%(what_label)s failed: %(error)s (error %(id)s)")
FAILED_ON = mark("%(what_label)s failed on %(where)s: %(error)s (error %(id)s)")
MOMENT_S = 3600.0
"""A moment for the same part is raised at most this often."""
QUEUED = 500


def fault_id(error: BaseException) -> str:
    """Eight hex digits from the error's type and the line it was raised on."""
    frames = traceback.extract_tb(error.__traceback__)
    where = f"{frames[-1].filename.rsplit('/', 1)[-1]}:{frames[-1].lineno}" if frames else ""
    return hashlib.sha256(f"{type(error).__name__}@{where}".encode()).hexdigest()[:8]


def said(error: BaseException) -> str:
    """The error as one line: its type and what it says."""
    text = str(error).strip().splitlines()[0] if str(error).strip() else ""
    return f"{type(error).__name__}: {text}" if text else type(error).__name__


@dataclass(frozen=True)
class _Change:
    key: str
    what: Raise | None
    """None to clear."""
    moment: bool = False


@dataclass
class Failures:
    _events: Events | None = None
    _queue: asyncio.Queue[_Change] = field(default_factory=lambda: asyncio.Queue(QUEUED))
    _open: set[str] = field(default_factory=set)
    _moments: dict[str, float] = field(default_factory=dict)

    async def attach(self, events: Events) -> None:
        """Raise failures as `events` from now on; those left open by an earlier run clear
        when their part next works."""
        self._events = events
        self._open = {e.key for e in await events.open(PREFIX)}

    def detach(self) -> None:
        self._events = None
        self._open.clear()
        self._moments.clear()
        while not self._queue.empty():
            self._queue.get_nowait()

    def failed(
        self,
        part: str,
        what: str,
        error: BaseException,
        *,
        where: str | None = None,
        logger: logging.Logger = log,
        moment: bool = False,
    ) -> str:
        """Log `error` with its traceback and raise it as an event; its ID. `what` is a
        marked message saying what was being done, without values; `where` what it was
        done for, as it is."""
        id = fault_id(error)
        logger.error(
            "%s failed%s (error %s)", what, f" on {where}" if where else "", id, exc_info=error
        )
        if self._events is None:
            return id
        key = f"{PREFIX}{part}"
        now = clock.time()
        if moment and now - self._moments.get(key, -MOMENT_S) < MOMENT_S:
            return id
        args: dict[str, Any] = {"what_label": what, "error": said(error), "id": id}
        if where:
            args["where"] = where
        message = FAILED_ON if where else FAILED
        change = _Change(key, Raise("failure", part, message, args), moment)
        try:
            self._queue.put_nowait(change)
        except asyncio.QueueFull:
            log.warning("too many failures waiting to be raised; %s isn't", key)
            return id
        if moment:
            self._moments[key] = now
        else:
            self._open.add(key)
        return id

    def worked(self, part: str) -> None:
        """`part` did what it does: its failure, if open, clears."""
        key = f"{PREFIX}{part}"
        if key not in self._open or self._events is None:
            return
        self._open.discard(key)
        try:
            self._queue.put_nowait(_Change(key, None))
        except asyncio.QueueFull:
            self._open.add(key)  # cleared the next time it works

    async def run(self) -> None:
        while True:
            await self._apply(await self._queue.get())

    async def settle(self) -> None:
        """Raise and clear what is waiting, now."""
        while not self._queue.empty():
            await self._apply(self._queue.get_nowait())

    async def _apply(self, change: _Change) -> None:
        events = self._events
        if events is None:
            return
        try:
            if change.what is None:
                await events.clear(change.key)
            elif change.moment:
                await events.moment(change.key, change.what)
            else:
                await events.raise_(change.key, change.what)
        except Exception:
            # Not raised through itself: that could go round for ever.
            log.exception("the failure %s couldn't be kept as an event", change.key)


failures = Failures()


@contextmanager
def attached_for_tests(events: Events) -> Iterator[Failures]:
    """`failures` raising to `events`, for a test; as it was afterwards."""
    failures._events = events
    try:
        yield failures
    finally:
        failures.detach()
