"""Plugins whose content is series: what they hold, and how they answer for it.

Such a plugin has one node, no levers, and usually no points. It holds the intervals it
has fetched, answers `series.get` from them, and sends each new interval or revision to
whoever subscribed. An interval that ended more than two days ago is let go: the core
has kept it by then.
"""

import asyncio
import time
from collections.abc import Callable, Iterable
from datetime import UTC, datetime, timedelta

from .cap import Message, Send
from .cap.messages import (
    Act,
    Describe,
    Described,
    Error,
    Fate,
    Read,
    SeriesData,
    SeriesGet,
    SeriesSubscribe,
    SeriesUpdate,
    Subscribe,
    Update,
    Values,
)
from .cap.model import Envelope, Interval, Node, Presence, Provider, SeriesInfo

HOME = "https://github.com/fnordpojk/thermaestro"
KEEP = timedelta(days=2)
"""How long after its end an interval is still held."""


class Refused(Exception):
    """The source refused the credentials or the request: asking again won't help."""


class SourceError(Exception):
    """The source answered with something that can't be used."""


def user_agent(name: str, version: str) -> str:
    """Who is asking, as the sources want to know: the plugin, and where to read about it."""
    return f"Thermaestro-{name}/{version} (+{HOME})"


class Held:
    """The intervals a plugin holds, by series and start."""

    def __init__(self, clock: Callable[[], float] = time.time) -> None:
        self._clock = clock
        self.by_series: dict[str, dict[datetime, Interval]] = {}
        self._changed = asyncio.Event()

    def keep(self, intervals: Iterable[Interval], revision: int | None = None) -> bool:
        """Keep new intervals, and changed ones as their next revision: one more than the
        one held, or `revision` where that is higher. True if anything changed."""
        changed = False
        for interval in intervals:
            held = self.by_series.setdefault(interval.series, {})
            current = held.get(interval.start)
            if current is None:
                if revision is not None and revision > interval.revision:
                    interval = interval.model_copy(update={"revision": revision})
                held[interval.start] = interval
            elif (current.value, current.end) != (interval.value, interval.end):
                next_revision = max(current.revision + 1, revision or 0)
                held[interval.start] = interval.model_copy(update={"revision": next_revision})
            else:
                continue
            changed = True
        self._forget_old()
        if changed:
            notify, self._changed = self._changed, asyncio.Event()
            notify.set()
        return changed

    def _forget_old(self) -> None:
        before = datetime.fromtimestamp(self._clock(), UTC) - KEEP
        for held in self.by_series.values():
            for start in [s for s, i in held.items() if i.end < before]:
                del held[start]

    def overlapping(self, series: str, start: datetime, end: datetime) -> list[Interval]:
        held = self.by_series.get(series, {})
        return [held[t] for t in sorted(held) if held[t].end > start and held[t].start < end]

    def all(self, series: str) -> list[Interval]:
        held = self.by_series.get(series, {})
        return [held[t] for t in sorted(held)]

    def known_until(self, series: str) -> datetime | None:
        held = self.by_series.get(series)
        return max(i.end for i in held.values()) if held else None

    async def follow(self, request: SeriesSubscribe, send: Send) -> None:
        """Send what is held, then each new interval or revision, until cancelled."""
        sent: dict[datetime, int] = {}
        while True:
            changed = self._changed
            held = self.by_series.get(request.series, {})
            new = [held[t] for t in sorted(held) if sent.get(t) != held[t].revision]
            if new:
                await send(SeriesUpdate(id=request.id, series=request.series, intervals=tuple(new)))
                sent.update((i.start, i.revision) for i in new)
            await changed.wait()

    async def answer(self, request: SeriesGet | SeriesSubscribe, send: Send) -> None:
        """A series request for a series this plugin offers."""
        if isinstance(request, SeriesGet):
            await send(
                SeriesData(
                    id=request.id,
                    series=request.series,
                    intervals=tuple(self.overlapping(request.series, request.start, request.end)),
                    known_until=self.known_until(request.series),
                )
            )
        else:
            await self.follow(request, send)


class SeriesPlugin:
    """The plugin side of a source of series: describing them and answering for them.
    Subclasses fetch, and say what they offer."""

    features: tuple[str, ...] = ("subscribe",)
    name: str
    version: str
    root: str
    """The plugin's one node."""
    label: str
    nothing_to_act_on = "a source of series has nothing to act on"

    def __init__(self, *, clock: Callable[[], float] = time.time) -> None:
        self._clock = clock
        self.held = Held(clock)
        self._ready: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._ready.add_done_callback(lambda f: f.cancelled() or f.exception())

    def series_infos(self) -> tuple[SeriesInfo, ...]:
        """The series offered; empty while not yet known."""
        raise NotImplementedError

    def provider(self) -> Provider:
        raise NotImplementedError

    def _settle(self) -> None:
        if not self._ready.done():
            self._ready.set_result(None)

    def _offered(self) -> set[str]:
        return {info.id for info in self.series_infos()}

    async def handle(self, request: Message, send: Send) -> None:
        match request:
            case Act():
                await send(
                    Fate(id=request.id, stage="dropped", t=_now(), detail=self.nothing_to_act_on)
                )
            case Read():
                await send(
                    Values(id=request.id, values=tuple(self._unknown(p) for p in request.points))
                )
            case Subscribe():
                await send(
                    Update(id=request.id, values=tuple(self._unknown(p) for p in request.points))
                )
                await asyncio.Event().wait()
            case Describe():
                await asyncio.shield(self._ready)
                await send(self.describe(request.id))
            case SeriesGet() | SeriesSubscribe():
                await asyncio.shield(self._ready)
                if request.series not in self._offered():
                    await send(Error(id=request.id, code="invalid", detail="no such series"))
                    return
                await self.held.answer(request, send)
            case _:
                await send(
                    Error(id=getattr(request, "id", None), code="unsupported", detail="not offered")
                )

    def describe(self, id: int | None = None) -> Described:
        return Described(
            id=id,
            nodes=(
                Node(
                    path=self.root,
                    kind="site",
                    presence=Presence(how="configured"),
                    label=self.label,
                ),
            ),
            series=self.series_infos(),
            provider=self.provider(),
        )

    def _unknown(self, path: str) -> Envelope:
        return Envelope.model_validate(
            {
                "point": path,
                "value": None,
                "t_observed": None,
                "t_received": _now(),
                "quality": "unknown",
                "source": "measured",
                "why": f"{self.label} has no points",
            }
        )


def _now() -> datetime:
    return datetime.now(UTC)
