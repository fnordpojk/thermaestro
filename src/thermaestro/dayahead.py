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

from .cap import Send
from .cap.messages import Health
from .cap.model import Interval, Publication
from .seriesplugin import Refused, SeriesPlugin, SourceError, user_agent

__all__ = ["DayAheadPlugin", "Refused", "SourceError"]

log = logging.getLogger(__name__)

GRACE = timedelta(hours=2)
"""How long after their publication time missing prices count as stale."""
JITTER_S = 300
"""Fetches are spread over this many seconds after a publication time, so installations
don't all ask at once."""


class DayAheadPlugin(SeriesPlugin):
    nothing_to_act_on = "a price source has nothing to act on"

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.time,
        poll_s: float = 600.0,
        retry_s: tuple[float, float] = (60.0, 1800.0),
        health_interval_s: float = 60.0,
    ) -> None:
        super().__init__(clock=clock)
        self._poll = poll_s
        self._retry = retry_s
        self._health_interval = health_interval_s
        self._last_traffic: datetime | None = None
        self._problem: str | None = None
        self._error: str | None = None

    # --- what each source supplies ---------------------------------------------------------

    def publication(self) -> Publication:
        raise NotImplementedError

    def zone(self) -> ZoneInfo:
        """Where the days begin and end."""
        raise NotImplementedError

    async def fetch(self, session: aiohttp.ClientSession, days: list[date]) -> list[Interval]:
        """The intervals published for these days; fewer if not all are out yet. Raises
        Refused, SourceError, or aiohttp's and asyncio's errors."""
        raise NotImplementedError

    # --- running ---------------------------------------------------------------------------

    async def events(self, send: Send) -> None:
        failures = 0
        timeout = aiohttp.ClientTimeout(total=60)
        headers = {"User-Agent": user_agent(self.name, self.version)}
        async with aiohttp.ClientSession(headers=headers, timeout=timeout) as s:
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

    async def _refresh(self, session: aiohttp.ClientSession) -> None:
        today = datetime.fromtimestamp(self._clock(), self.zone()).date()
        intervals = await self.fetch(session, [today, today + timedelta(days=1)])
        self._last_traffic = _now()
        self.held.keep(intervals)

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
        """Whether tomorrow's prices are all in: tomorrow as the market's day, which is
        what each publication covers. Portugal's day ends an hour after the European
        auction's, so its local tomorrow is never complete before the next auction."""
        zone = ZoneInfo(self.publication().tz)
        today = datetime.fromtimestamp(self._clock(), zone).date()
        end = datetime.combine(today + timedelta(days=2), datetime.min.time(), zone)
        offered = self._offered()
        return bool(offered) and all(
            (until := self.held.known_until(s)) is not None and until >= end for s in offered
        )

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


def _now() -> datetime:
    return datetime.now(UTC)
