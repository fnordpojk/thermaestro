"""Simulated time for the whole stack: an event loop whose clock jumps ahead whenever
nothing is ready to run, so days pass in seconds.

The loop's clock and `thermaestro.clock` read the same `VirtualClock`. Work handed to a
thread (the database's) runs in real time, and the clock waits for it: time doesn't jump
while a thread is busy, or a reply would arrive "hours" late.
"""

import asyncio
import selectors
from collections.abc import Awaitable, Callable, Coroutine
from concurrent.futures import Executor
from datetime import datetime
from typing import Any

from thermaestro import clock

STALL_S = 2.0
"""Real seconds a loop with nothing scheduled waits for a thread before it gives up."""


class Stalled(RuntimeError):
    """Nothing is scheduled and nothing is running: every task waits on something that
    will never come."""


class VirtualClock:
    def __init__(self, start: datetime) -> None:
        self._epoch = start.timestamp()
        self.elapsed = 0.0

    def monotonic(self) -> float:
        return self.elapsed

    def time(self) -> float:
        return self._epoch + self.elapsed


class _Jumping:
    """The loop's selector, which moves the clock instead of waiting when it may."""

    def __init__(self, inner: selectors.BaseSelector, loop: "VirtualLoop") -> None:
        self._inner = inner
        self._loop = loop

    def select(self, timeout: float | None = None) -> list[Any]:
        if self._loop.busy:
            # A thread is working: wait for it in real time; the clock stands still.
            wait = 0.01 if timeout is None else min(timeout, 0.01)
            return self._inner.select(wait)
        events = self._inner.select(0)
        if events or timeout == 0:
            return events
        if timeout is None:
            # Only a thread can wake it now (one not started through run_in_executor).
            events = self._inner.select(STALL_S)
            if not events:
                raise Stalled("the simulation stalled: nothing is scheduled or running")
            return events
        self._loop.clock.elapsed += timeout
        return []

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


class VirtualLoop(asyncio.SelectorEventLoop):
    def __init__(self, clock: VirtualClock) -> None:
        super().__init__()
        self.clock = clock
        self.busy = 0
        """Thread jobs in flight."""
        inner: selectors.BaseSelector = self.__dict__["_selector"]
        self.__dict__["_selector"] = _Jumping(inner, self)

    def time(self) -> float:
        return self.clock.monotonic()

    def run_in_executor(  # type: ignore[override]
        self, executor: Executor | None, func: Callable[..., Any], *args: Any
    ) -> asyncio.Future[Any]:
        future = super().run_in_executor(executor, func, *args)
        self.busy += 1

        def done(_: asyncio.Future[Any]) -> None:
            self.busy -= 1

        future.add_done_callback(done)
        return future


def simulate[T](main: Callable[[], Coroutine[Any, Any, T]], start: datetime) -> T:
    """Run `main` in simulated time from `start`, with Thermaestro's clock following."""
    virtual = VirtualClock(start)
    loop = VirtualLoop(virtual)
    try:
        with clock.simulated(virtual):
            return loop.run_until_complete(main())
    finally:
        try:
            loop.run_until_complete(loop.shutdown_asyncgens())
            loop.run_until_complete(loop.shutdown_default_executor())
        finally:
            loop.close()


async def sleep_until(t: float) -> None:
    """Sleep until `clock.time()` reaches `t`."""
    await asyncio.sleep(max(0.0, t - clock.time()))


async def every(seconds: float, act: Callable[[], Awaitable[object] | None]) -> None:
    """Call `act` every `seconds` of simulated time, on the boundaries of `seconds`."""
    while True:
        now = clock.time()
        await sleep_until((now // seconds + 1) * seconds)
        result = act()
        if result is not None:
            await result
