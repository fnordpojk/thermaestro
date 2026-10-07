"""Weather forecast plugins: forecasts for one location, fetched from a web API and kept
current.

A plugin of this kind offers a series per quantity its provider gives (`temperature`,
`dew_point`, …, the vocabulary's `WEATHER`), in those units, each interval as the
provider has it: a value for an instant holds from its time to the next one, and amounts
and means cover the period the provider says. Nothing is derived here; the core does
that, and says so.

It asks again when the provider's answer expires, or every hour where the provider
doesn't say. A newer run's changed values become new revisions, numbered by the run's
time in minutes, so a restarted plugin's values still replace what the core kept from
an older run.

The location goes out rounded to three decimals, about 100 m: finer than any model's
grid, and no closer to the house than that.
"""

import asyncio
import logging
import secrets
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime

import aiohttp

from .cap import Send
from .cap.messages import Health
from .cap.model import Duration, ForecastModel, Interval, Knowledge, SeriesInfo, Step
from .cap.vocabulary import WEATHER
from .seriesplugin import Refused, SeriesPlugin, SourceError, user_agent

log = logging.getLogger(__name__)

AHEAD = timedelta(hours=6)
"""A forecast that doesn't reach this far ahead is stale."""
JITTER_S = 120
DECIMALS = 3


@dataclass
class Fetched:
    intervals: list[Interval] | None
    """None: the provider has nothing newer than what is held."""
    issued: datetime | None = None
    """When the provider made this run, where it says."""
    expires: datetime | None = None
    """Until when the provider asks not to be asked again, where it says."""


def iso(span: timedelta) -> Duration:
    """A duration in days, hours and minutes, as ISO 8601 writes it: `PT1H`, `P2DT6H`."""
    minutes = max(1, round(span.total_seconds() / 60))
    days, rest = divmod(minutes, 1440)
    hours, minutes = divmod(rest, 60)
    time_part = (f"{hours}H" if hours else "") + (f"{minutes}M" if minutes else "")
    if days:
        return f"P{days}D" + (f"T{time_part}" if time_part else "")
    return f"PT{time_part}"


def http_date(text: str | None) -> datetime | None:
    """A date as HTTP headers write it (`Expires`), or None."""
    if not text:
        return None
    try:
        return parsedate_to_datetime(text)
    except (TypeError, ValueError):
        return None


def instants(
    series: str,
    times: Sequence[datetime],
    values: Sequence[float | None],
    issued: datetime | None,
    *,
    quantity: str | None = None,
) -> list[Interval]:
    """Values for instants as intervals from each time to the next; the last one as long
    as the step before it. A missing value leaves its interval out. The series is named
    after its quantity unless it says otherwise."""
    out = []
    for n, (start, value) in enumerate(zip(times, values, strict=True)):
        if value is None:
            continue
        if n + 1 < len(times):
            end = times[n + 1]
        elif n > 0:
            end = start + (start - times[n - 1])
        else:
            end = start + timedelta(hours=1)
        out.append(interval(series, start, end, value, issued, quantity=quantity))
    return out


def interval(
    series: str,
    start: datetime,
    end: datetime,
    value: float,
    issued: datetime | None,
    *,
    quantity: str | None = None,
) -> Interval:
    return Interval(
        series=series,
        start=start,
        end=end,
        value=round(value, 3),
        unit=WEATHER[quantity or series],
        status="forecast",
        t_published=issued,
    )


def steps(intervals: Iterable[Interval], since: datetime) -> tuple[Step, ...]:
    """The steps of a forecast by lead time from `since`: each step and the lead it lasts
    to, the last one to the horizon."""
    out: list[Step] = []
    current: timedelta | None = None
    until: timedelta | None = None
    for i in intervals:
        if i.end <= since:
            continue
        length = i.end - i.start
        if current is not None and length != current and until is not None:
            out.append(Step(step=iso(current), until=iso(until)))
        if length != current:
            current = length
        until = i.end - since
    if current is not None:
        out.append(Step(step=iso(current)))
    return tuple(out)


class ForecastPlugin(SeriesPlugin):
    nothing_to_act_on = "a weather provider has nothing to act on"

    def __init__(
        self,
        latitude: float,
        longitude: float,
        *,
        clock: Callable[[], float] = time.time,
        every_s: float = 3600.0,
        min_wait_s: float = 300.0,
        retry_s: tuple[float, float] = (60.0, 1800.0),
        health_interval_s: float = 60.0,
        jitter_s: int = JITTER_S,
    ) -> None:
        super().__init__(clock=clock)
        self.latitude = round(latitude, DECIMALS)
        self.longitude = round(longitude, DECIMALS)
        self._every = every_s
        self._min_wait = min_wait_s
        self._jitter = jitter_s
        self._retry = retry_s
        self._health_interval = health_interval_s
        self._last_traffic: datetime | None = None
        self._problem: str | None = None
        self._error: str | None = None
        self._issued: datetime | None = None
        self._infos: tuple[SeriesInfo, ...] = ()

    # --- what each provider supplies --------------------------------------------------------

    def models(self) -> Knowledge[tuple[ForecastModel, ...]]:
        return Knowledge[tuple[ForecastModel, ...]]()

    def updates(self) -> Knowledge[Duration]:
        return Knowledge[Duration]()

    async def fetch(self, session: aiohttp.ClientSession) -> Fetched:
        """The provider's latest forecast. Raises Refused, SourceError, or aiohttp's and
        asyncio's errors."""
        raise NotImplementedError

    # --- describing --------------------------------------------------------------------------

    def series_infos(self) -> tuple[SeriesInfo, ...]:
        return self._infos

    def _describe_held(self) -> tuple[SeriesInfo, ...]:
        """The series as the held forecast shows them: the quantities given, with their
        steps and horizon from the latest run."""
        since = self._issued or datetime.fromtimestamp(self._clock(), UTC)
        out = []
        for quantity, unit in WEATHER.items():
            held = self.held.all(quantity)
            if not held:
                continue
            shape = steps(held, since)
            finest = min(i.end - i.start for i in held)
            percentiles: tuple[int, ...] = ()
            if quantity == "temperature" and self.held.all("temperature.p10"):
                percentiles = (10, 90)
            out.append(
                SeriesInfo(
                    id=quantity,
                    kind="forecast",
                    role="weather",
                    unit=unit,
                    resolution=iso(finest),
                    horizon=iso(max(i.end for i in held) - since),
                    quantity=quantity,
                    steps=shape if len(shape) > 1 else (),
                    models=self.models(),
                    updates=self.updates(),
                    percentiles=percentiles,
                )
            )
        return tuple(out)

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
                        fetched = await self.fetch(s)
                    except Refused as e:
                        self._problem = str(e)
                        self._settle()
                        await send(self._health())
                        await asyncio.Event().wait()  # until the settings change
                    except (aiohttp.ClientError, TimeoutError, SourceError) as e:
                        failures += 1
                        self._error = f"{type(e).__name__}: {e}"
                        log.warning("%s: fetching the forecast failed: %s", self.name, self._error)
                        if not self._infos:
                            raise  # nothing to describe yet; the host restarts the plugin
                        self._settle()
                        await send(self._health())
                        pause = min(self._retry[1], self._retry[0] * 2 ** (failures - 1))
                        await asyncio.sleep(pause)
                        continue
                    failures, self._error = 0, None
                    self._keep(fetched)
                    self._settle()
                    await send(self._health())
                    await asyncio.sleep(self._wait(fetched.expires))
            finally:
                health.cancel()

    def _keep(self, fetched: Fetched) -> None:
        now = datetime.fromtimestamp(self._clock(), UTC)
        self._last_traffic = now
        if fetched.intervals is None:
            return
        issued = fetched.issued or now
        self.held.keep(fetched.intervals, revision=int(issued.timestamp() // 60))
        self._issued = issued
        if not self._infos:
            self._infos = self._describe_held()

    def _wait(self, expires: datetime | None) -> float:
        """Seconds until the next fetch: once the answer expires, or after the usual
        interval where it doesn't say; never sooner than a few minutes."""
        now = self._clock()
        due = expires.timestamp() - now if expires is not None else self._every
        return max(self._min_wait, due) + (secrets.randbelow(self._jitter) if self._jitter else 0)

    async def _send_health(self, send: Send) -> None:
        await asyncio.shield(self._ready)
        while True:
            await asyncio.sleep(self._health_interval)
            await send(self._health())

    def _health(self) -> Health:
        needed = datetime.fromtimestamp(self._clock(), UTC) + AHEAD
        stale = tuple(
            sorted(
                info.id
                for info in self._infos
                if (until := self.held.known_until(info.id)) is None or until < needed
            )
        )
        return Health(
            t=datetime.now(UTC),
            unit=self.root,
            state="down" if self._problem or self._error else "up",
            last_traffic=self._last_traffic,
            needs_user_action=self._problem,
            stale=stale,
        )
