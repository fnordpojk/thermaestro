"""Day-ahead price plugins: price series fetched by day from a web API and kept current.

A plugin of this kind has no devices, only series. It fetches what is published when it
starts, then each day once the next day's prices are due, asking again every few minutes
until they come. A source that refuses the credentials isn't asked again: that takes the
user, and a changed setting restarts the plugin.

An interval is kept as first fetched; one fetched again with another value becomes its
next revision.
"""

import asyncio
import logging
import secrets
import time
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import aiohttp

from .cap import Message, Send
from .cap.messages import (
    Act,
    Describe,
    Described,
    Error,
    Fate,
    Health,
    Read,
    SeriesData,
    SeriesGet,
    SeriesSubscribe,
    SeriesUpdate,
    Subscribe,
    Update,
    Values,
)
from .cap.model import Envelope, Interval, Node, Presence, Provider, Publication, SeriesInfo

log = logging.getLogger(__name__)

HOME = "https://github.com/fnordpojk/thermaestro"
GRACE = timedelta(hours=2)
"""How long after their publication time missing prices count as stale."""
JITTER_S = 300
"""Fetches are spread over this many seconds after a publication time, so installations
don't all ask at once."""


class Refused(Exception):
    """The source refused the credentials: asking again won't help."""


class SourceError(Exception):
    """The source answered with something that isn't prices."""


class DayAheadPlugin:
    features: tuple[str, ...] = ("subscribe",)
    name: str
    version: str
    root: str
    """The plugin's one node."""
    label: str

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.time,
        poll_s: float = 600.0,
        retry_s: tuple[float, float] = (60.0, 1800.0),
        health_interval_s: float = 60.0,
    ) -> None:
        self._clock = clock
        self._poll = poll_s
        self._retry = retry_s
        self._health_interval = health_interval_s
        self.held: dict[str, dict[datetime, Interval]] = {}
        self._ready: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._ready.add_done_callback(lambda f: f.cancelled() or f.exception())
        self._changed = asyncio.Event()
        self._last_traffic: datetime | None = None
        self._problem: str | None = None
        self._error: str | None = None

    # --- what each source supplies ---------------------------------------------------------

    def series_infos(self) -> tuple[SeriesInfo, ...]:
        """The series offered; empty while not yet known (a source may first have to be
        asked what currency it prices in)."""
        raise NotImplementedError

    def provider(self) -> Provider:
        raise NotImplementedError

    def publication(self) -> Publication:
        raise NotImplementedError

    def zone(self) -> ZoneInfo:
        """Where the days begin and end."""
        raise NotImplementedError

    async def fetch(self, session: aiohttp.ClientSession, days: list[date]) -> list[Interval]:
        """The intervals published for these days; fewer if not all are out yet. Raises
        Refused, SourceError, or aiohttp's and asyncio's errors."""
        raise NotImplementedError

    # --- the plugin interface ---------------------------------------------------------------

    async def events(self, send: Send) -> None:
        failures = 0
        timeout = aiohttp.ClientTimeout(total=60)
        agent = f"Thermaestro-{self.name}/{self.version} (+{HOME})"
        async with aiohttp.ClientSession(headers={"User-Agent": agent}, timeout=timeout) as s:
            health = asyncio.create_task(self._send_health(send))
            try:
                while True:
                    try:
                        await self._refresh(s)
                        failures, self._error = 0, None
                    except Refused as e:
                        self._problem = str(e)
                        self._settle()
                        await send(self._health())
                        await asyncio.Event().wait()  # until the settings change
                    except (aiohttp.ClientError, TimeoutError, SourceError) as e:
                        failures += 1
                        self._error = f"{type(e).__name__}: {e}"
                        log.warning("%s: fetching prices failed: %s", self.name, self._error)
                        if not self.series_infos():
                            raise  # nothing to describe yet; the host restarts the plugin
                        self._settle()
                        await send(self._health())
                        pause = min(self._retry[1], self._retry[0] * 2 ** (failures - 1))
                        await asyncio.sleep(pause)
                        continue
                    self._settle()
                    await send(self._health())
                    await asyncio.sleep(self._wait())
            finally:
                health.cancel()

    async def handle(self, request: Message, send: Send) -> None:
        match request:
            case Act():
                await send(
                    Fate(
                        id=request.id,
                        stage="dropped",
                        t=_now(),
                        detail="a price source has nothing to act on",
                    )
                )
            case Read():
                await send(Values(id=request.id, values=tuple(_unknown(p) for p in request.points)))
            case Subscribe():
                await send(Update(id=request.id, values=tuple(_unknown(p) for p in request.points)))
                await asyncio.Event().wait()
            case Describe():
                await asyncio.shield(self._ready)
                await send(self.describe(request.id))
            case SeriesGet():
                await asyncio.shield(self._ready)
                if request.series not in self._offered():
                    await send(Error(id=request.id, code="invalid", detail="no such series"))
                    return
                held = self._overlapping(request.series, request.start, request.end)
                await send(
                    SeriesData(
                        id=request.id,
                        series=request.series,
                        intervals=tuple(held),
                        known_until=self._known_until(request.series),
                    )
                )
            case SeriesSubscribe():
                await asyncio.shield(self._ready)
                if request.series not in self._offered():
                    await send(Error(id=request.id, code="invalid", detail="no such series"))
                    return
                await self._follow(request, send)
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

    # --- fetching ----------------------------------------------------------------------------

    async def _refresh(self, session: aiohttp.ClientSession) -> None:
        today = datetime.fromtimestamp(self._clock(), self.zone()).date()
        intervals = await self.fetch(session, [today, today + timedelta(days=1)])
        self._last_traffic = _now()
        if self._keep(intervals):
            changed, self._changed = self._changed, asyncio.Event()
            changed.set()

    def _keep(self, intervals: list[Interval]) -> bool:
        """Keep new intervals, and revisions of changed ones. True if anything changed."""
        changed = False
        for interval in intervals:
            held = self.held.setdefault(interval.series, {})
            current = held.get(interval.start)
            if current is None:
                held[interval.start] = interval
            elif (current.value, current.end) != (interval.value, interval.end):
                held[interval.start] = interval.model_copy(
                    update={"revision": current.revision + 1}
                )
            else:
                continue
            changed = True
        return changed

    def _settle(self) -> None:
        if not self._ready.done():
            self._ready.set_result(None)

    def _wait(self) -> float:
        """Seconds until the next fetch: the next publication time once tomorrow's prices
        are held; until then, a few minutes after today's is due."""
        publication = self.publication()
        zone = ZoneInfo(publication.tz)
        now = datetime.fromtimestamp(self._clock(), zone)
        hour, minute = map(int, publication.daily_after.split(":"))
        due = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        jitter = secrets.randbelow(JITTER_S)
        if self._holds_tomorrow():
            target = due + timedelta(days=1) if now >= due else due
            return max(self._poll, (target - now).total_seconds() + jitter)
        if now < due:
            return (due - now).total_seconds() + jitter
        return self._poll

    def _holds_tomorrow(self) -> bool:
        zone = self.zone()
        today = datetime.fromtimestamp(self._clock(), zone).date()
        end = datetime.combine(today + timedelta(days=2), datetime.min.time(), zone)
        offered = self._offered()
        return bool(offered) and all(
            (until := self._known_until(s)) is not None and until >= end for s in offered
        )

    # --- answering ---------------------------------------------------------------------------

    def _offered(self) -> set[str]:
        return {info.id for info in self.series_infos()}

    def _overlapping(self, series: str, start: datetime, end: datetime) -> list[Interval]:
        held = self.held.get(series, {})
        return [held[t] for t in sorted(held) if held[t].end > start and held[t].start < end]

    def _known_until(self, series: str) -> datetime | None:
        held = self.held.get(series)
        return max(i.end for i in held.values()) if held else None

    async def _follow(self, request: SeriesSubscribe, send: Send) -> None:
        """Send what is held, then each new interval or revision."""
        sent: dict[datetime, int] = {}
        while True:
            changed = self._changed
            held = self.held.get(request.series, {})
            new = [held[t] for t in sorted(held) if sent.get(t) != held[t].revision]
            if new:
                await send(SeriesUpdate(id=request.id, series=request.series, intervals=tuple(new)))
                sent.update((i.start, i.revision) for i in new)
            await changed.wait()

    async def _send_health(self, send: Send) -> None:
        await asyncio.shield(self._ready)
        while True:
            await asyncio.sleep(self._health_interval)
            await send(self._health())

    def _health(self) -> Health:
        stale: tuple[str, ...] = ()
        publication = self.publication() if self.series_infos() else None
        if publication is not None and not self._holds_tomorrow():
            zone = ZoneInfo(publication.tz)
            now = datetime.fromtimestamp(self._clock(), zone)
            hour, minute = map(int, publication.daily_after.split(":"))
            if now >= now.replace(hour=hour, minute=minute, second=0, microsecond=0) + GRACE:
                stale = tuple(sorted(self._offered()))
        return Health(
            t=_now(),
            unit=self.root,
            state="down" if self._problem or self._error else "up",
            last_traffic=self._last_traffic,
            needs_user_action=self._problem,
            stale=stale,
        )


def _unknown(path: str) -> Envelope:
    return Envelope.model_validate(
        {
            "point": path,
            "value": None,
            "t_observed": None,
            "t_received": _now(),
            "quality": "unknown",
            "source": "measured",
            "why": "a price source has no points",
        }
    )


def _now() -> datetime:
    return datetime.now(UTC)
