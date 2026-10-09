"""The time Thermaestro reads: the system's, unless a simulation has set its own.

Everything that runs with the plant in simulated time reads the clock here rather than the
`time` module: `monotonic` for durations, `time` and `now` for the wall clock. A simulation
installs a `Source` with `simulated`; nothing else ever does.
"""

import time as _time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Protocol


class Source(Protocol):
    def monotonic(self) -> float: ...

    def time(self) -> float: ...


_source: Source | None = None


def monotonic() -> float:
    """Seconds that only go forward, for durations."""
    return _time.monotonic() if _source is None else _source.monotonic()


def time() -> float:
    """Seconds since the epoch."""
    return _time.time() if _source is None else _source.time()


def now() -> datetime:
    """The wall clock's time, in UTC."""
    return datetime.now(UTC) if _source is None else datetime.fromtimestamp(time(), UTC)


@contextmanager
def simulated(source: Source) -> Iterator[None]:
    """Read `source` instead of the system's clock, until the block ends."""
    global _source  # the one place the source is set
    if _source is not None:
        raise RuntimeError("a simulated clock is already in use")
    _source = source
    try:
        yield
    finally:
        _source = None
