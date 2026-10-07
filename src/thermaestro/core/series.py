"""Series: prices, grid rules and forecasts, kept interval by interval.

Each plugin instance's series are fetched when it has described them, from a day back to
two days ahead, and followed from then on: a plugin sends new intervals as they are
published, and revisions of published ones. An interval keeps its latest revision.

A series is stale when what should be there isn't: prices for tomorrow two hours after
the time they are published each day; a forecast that doesn't reach six hours ahead; any
other series that doesn't reach now.
"""

import asyncio
import contextlib
import logging
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

from ..cap import Closed, Link
from ..cap.client import CapError
from ..cap.model import Interval, SeriesInfo
from ..store import Database, Transaction

log = logging.getLogger(__name__)

PUBLICATION_GRACE = timedelta(hours=2)
FORECAST_AHEAD = timedelta(hours=6)
BACK = timedelta(days=1)
AHEAD = timedelta(days=2)

Freshness = Literal["fresh", "stale", "empty"]
Row = tuple[float, float, float, str, str, str, int, float | None, str | None, str | None]


@dataclass(frozen=True, slots=True)
class Key:
    instance: str
    series: str


@dataclass
class Followed:
    info: SeriesInfo
    known_until: float | None = None
    """The end of the last interval held, seconds since the epoch."""
    updated: float | None = None
    error: str | None = None


class Series:
    def __init__(self, db: Database, clock: Callable[[], float] = time.time) -> None:
        self._db = db
        self._clock = clock
        self.followed: dict[Key, Followed] = {}
        self.listeners: list[Callable[[Key], None]] = []
        """Told when a series has new or revised intervals."""

    # --- keeping intervals ---------------------------------------------------------------

    async def put(self, instance: str, intervals: Iterable[Interval]) -> int:
        """Keep intervals; an interval already held is replaced by a revision that isn't
        older. Returns how many were kept."""
        rows = [
            (
                instance,
                i.series,
                i.start.timestamp(),
                i.end.timestamp(),
                i.value,
                i.unit,
                i.vat,
                i.status,
                i.revision,
                i.t_published.timestamp() if i.t_published else None,
                i.source,
                i.why,
            )
            for i in intervals
        ]
        if not rows:
            return 0

        def write(t: Transaction) -> None:
            t.executemany(
                "INSERT INTO intervals (instance, series, start, end, value, unit, vat, status,"
                " revision, published, source, why) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT (instance, series, start) DO UPDATE SET end = excluded.end,"
                " value = excluded.value, unit = excluded.unit, vat = excluded.vat,"
                " status = excluded.status, revision = excluded.revision,"
                " published = excluded.published, source = excluded.source, why = excluded.why"
                " WHERE excluded.revision >= intervals.revision",
                rows,
            )

        await self._db.run(write)
        now = self._clock()
        touched = {Key(instance, row[1]) for row in rows}
        for key in touched:
            ends = [row[3] for row in rows if row[1] == key.series]
            followed = self.followed.get(key)
            if followed is not None:
                followed.known_until = max(followed.known_until or 0.0, *ends)
                followed.updated = now
                followed.error = None
        for key in touched:
            for listener in self.listeners:
                listener(key)
        return len(rows)

    async def get(self, key: Key, start: float, end: float) -> list[Interval]:
        """The intervals that overlap [start, end), in order."""

        def read(t: Transaction) -> list[Row]:
            return list(
                t.execute(
                    "SELECT start, end, value, unit, vat, status, revision, published, source,"
                    " why FROM intervals WHERE instance = ? AND series = ? AND end > ?"
                    " AND start < ? ORDER BY start",
                    (key.instance, key.series, start, end),
                )
            )

        return [_interval(key.series, row) for row in await self._db.run(read)]

    # --- following a plugin's series -----------------------------------------------------

    def describe(self, instance: str, infos: Iterable[SeriesInfo]) -> None:
        """What an instance offers now; series it no longer offers are dropped."""
        offered = {Key(instance, info.id): info for info in infos}
        for key in [k for k in self.followed if k.instance == instance and k not in offered]:
            del self.followed[key]
        for key, info in offered.items():
            current = self.followed.get(key)
            if current is None:
                self.followed[key] = Followed(info)
            else:
                current.info = info

    async def follow(self, instance: str, link: Link, infos: Iterable[SeriesInfo]) -> None:
        """Fetch each series, then keep it current, until cancelled or the link closes."""
        infos = list(infos)
        self.describe(instance, infos)
        if not infos:
            return
        now = datetime.fromtimestamp(self._clock(), UTC)
        for info in infos:
            try:
                data = await link.series_get(info.id, now - BACK, now + AHEAD, timeout=60)
                await self.put(instance, data.intervals)
            except (CapError, TimeoutError) as e:
                self._failed(Key(instance, info.id), e)
        tasks = [asyncio.create_task(self._updates(instance, link, info)) for info in infos]
        try:
            await asyncio.gather(*tasks)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _updates(self, instance: str, link: Link, info: SeriesInfo) -> None:
        with contextlib.suppress(Closed):
            async with link.series_subscribe(info.id) as subscription:
                while True:
                    try:
                        update = await subscription.next(timeout=3600)
                    except TimeoutError:
                        continue  # nothing new for an hour; staleness tells if that matters
                    await self.put(instance, update.intervals)

    def _failed(self, key: Key, error: Exception) -> None:
        followed = self.followed.get(key)
        if followed is not None:
            followed.error = f"{type(error).__name__}: {error}"
        log.warning("series %s of %s: %s", key.series, key.instance, error)

    # --- staleness -----------------------------------------------------------------------

    def freshness(self, key: Key) -> tuple[Freshness, str | None]:
        """Whether a series holds what it should by now, and if not, what is missing."""
        followed = self.followed.get(key)
        if followed is None or followed.known_until is None:
            return "empty", followed.error if followed else None
        now = datetime.fromtimestamp(self._clock(), UTC)
        until = datetime.fromtimestamp(followed.known_until, UTC)
        info = followed.info
        if info.publication is not None:
            zone = ZoneInfo(info.publication.tz)
            local = now.astimezone(zone)
            hour, minute = map(int, info.publication.daily_after.split(":"))
            due = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
            days = 2 if local >= due + PUBLICATION_GRACE else 1
            midnight = (local + timedelta(days=days)).replace(
                hour=0, minute=0, second=0, microsecond=0
            )
            needed = midnight.replace(tzinfo=None).replace(tzinfo=zone)
            if until < needed:
                if days == 2:
                    return (
                        "stale",
                        f"nothing for tomorrow yet, though due at {info.publication.daily_after}",
                    )
                return "stale", "nothing for the rest of today"
            return "fresh", None
        if info.kind == "forecast":
            if until < now + FORECAST_AHEAD:
                return "stale", "the forecast doesn't reach 6 hours ahead"
            return "fresh", None
        if until < now:
            return "stale", "nothing for now"
        return "fresh", None


def _interval(series: str, row: Row) -> Interval:
    start, end, value, unit, vat, status, revision, published, source, why = row
    return Interval.model_validate(
        {
            "series": series,
            "start": datetime.fromtimestamp(start, UTC),
            "end": datetime.fromtimestamp(end, UTC),
            "value": value,
            "unit": unit,
            "vat": vat,
            "status": status,
            "revision": revision,
            "t_published": datetime.fromtimestamp(published, UTC) if published else None,
            "source": source,
            "why": why,
        }
    )
