"""Weather: the forecast for each quantity from the provider chosen for it, values the
providers don't give derived from those they do, and how each provider has done at this
house.

**Providers** are the forecast series the plugins offer, grouped by where they come from:
a weather plugin's instance (`met`), or one of Home Assistant's weather entities
(`ha:weather.forecast_home`).

**The choice** (the `weather` setting): a main provider, another for any quantity, and
fallbacks that stand in while the chosen one's forecast is missing or stale.

**Derived values**, computed here and marked as derived, never better than their inputs:
the dew point from temperature and humidity and back (Magnus, as for the sensors), and
global sunlight from cloud cover and the sun's height (`sun`).

**Scoring at the house:** every few minutes, each provider's temperature and dew point
for the whole hours 1, 6, 24 and 48 hours ahead are kept as forecast then; once that hour
has come, the outdoor reference measured then (`site:outdoor/<quantity>`) is kept beside
them. Bias and mean absolute error follow by lead time, over the last 30 days. Every
provider is scored, chosen or not, so several can run side by side.
"""

import asyncio
import logging
import math
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from ..cap.model import Interval, Provider, SeriesInfo
from ..store import Database, Location, Transaction, WeatherChoice
from .sensors import SITE, dew_point, relative_humidity
from .series import Key, Series
from .sun import sunlight
from .values import Key as PointKey
from .values import Values

if TYPE_CHECKING:
    from .host import PluginHost

log = logging.getLogger(__name__)

LEADS_H = (1, 6, 24, 48)
SCORED = ("temperature", "dew_point")
WINDOW_DAYS = 30
ENOUGH = 24
"""Comparisons at one lead time before its scores are shown: a day's hours."""
OBSERVED_WITHIN_S = 3600.0
"""How far before the hour its measurement is looked for: a sensor that reports only on
a change may be quiet for that long and still be right."""
KEEP_LEADS_DAYS = 90
KEEP_FORECASTS_DAYS = 7
"""Forecast intervals are dropped this long after they end; the kept leads stay."""
INSTANT = frozenset(
    {
        "temperature",
        "temperature.p10",
        "temperature.p90",
        "dew_point",
        "relative_humidity",
        "cloud_cover",
        "wind_speed",
        "wind_direction",
        "pressure",
    }
)
"""Quantities given for an instant, so interpolated between two; the others are means or
amounts over their interval."""

DERIVABLE: dict[str, tuple[str, ...]] = {
    "dew_point": ("temperature", "relative_humidity"),
    "relative_humidity": ("temperature", "dew_point"),
    "irradiance.global": ("cloud_cover",),
}
"""What can be derived, and from what."""


@dataclass(frozen=True)
class Span:
    start: datetime
    end: datetime
    value: float


@dataclass
class Source:
    key: str
    instance: str
    entity: str | None
    """For Home Assistant: the weather entity."""
    provider: Provider | None
    infos: dict[str, SeriesInfo] = field(default_factory=dict)
    """By quantity."""

    def series(self, quantity: str) -> Key:
        id = f"{self.entity}/{quantity}" if self.entity else quantity
        return Key(self.instance, id)

    @property
    def label(self) -> str:
        name = self.provider.name if self.provider else self.instance
        return f"{name}: {self.entity}" if self.entity else name


@dataclass
class Forecast:
    quantity: str
    source: str | None
    """The provider it came from; None where none has it."""
    derived: bool = False
    fallback: bool = False
    stale: bool = False
    spans: list[Span] = field(default_factory=list)


@dataclass(frozen=True)
class Score:
    source: str
    quantity: str
    lead_h: int
    n: int
    bias: float
    mae: float


def value_at(spans: list[Span], t: datetime, instant: bool) -> float | None:
    """The value at a time: interpolated between two instants, or the interval's own."""
    for n, span in enumerate(spans):
        if span.start <= t < span.end:
            following = spans[n + 1] if n + 1 < len(spans) else None
            if instant and following is not None and following.start == span.end:
                share = (t - span.start) / (span.end - span.start)
                return span.value + share * (following.value - span.value)
            return span.value
    return None


def next_hour(t: datetime) -> datetime:
    whole = t.replace(minute=0, second=0, microsecond=0)
    return whole if whole == t else whole + timedelta(hours=1)


class Weather:
    def __init__(
        self,
        db: Database,
        series: Series,
        values: Values,
        host: "PluginHost | None" = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._db = db
        self._series = series
        self._values = values
        self._host = host
        self._clock = clock

    # --- the providers ---------------------------------------------------------------------

    def sources(self) -> dict[str, Source]:
        """Every provider whose forecasts are followed, by key."""
        out: dict[str, Source] = {}
        for key, followed in self._series.followed.items():
            info = followed.info
            if info.kind != "forecast" or not info.quantity:
                continue
            entity = None
            if key.series != info.quantity:
                entity = key.series.removesuffix(f"/{info.quantity}")
            source_key = f"{key.instance}:{entity}" if entity else key.instance
            source = out.get(source_key)
            if source is None:
                source = out[source_key] = Source(
                    source_key, key.instance, entity, self._provider(key.instance)
                )
            source.infos[info.quantity] = info
        return dict(sorted(out.items()))

    def _provider(self, instance: str) -> Provider | None:
        if self._host is None:
            return None
        found = self._host.instances.get(instance)
        described = found.described if found else None
        return described.provider if described else None

    async def choice(self) -> WeatherChoice:
        return await self._db.get(WeatherChoice) or WeatherChoice()

    @staticmethod
    def order(choice: WeatherChoice, quantity: str) -> list[str]:
        """The providers to try for a quantity, in order."""
        out: list[str] = []
        for key in (choice.quantities.get(quantity) or choice.main, *choice.fallbacks):
            if key and key not in out:
                out.append(key)
        return out

    def has(self, source: Source, quantity: str) -> str | None:
        """`offered`, `derived`, or None where the provider can't give it."""
        if quantity in source.infos:
            return "offered"
        needed = DERIVABLE.get(quantity)
        if needed and all(q in source.infos for q in needed):
            return "derived"
        return None

    def fresh(self, source: Source, quantity: str) -> bool:
        how = self.has(source, quantity)
        needed = (quantity,) if how == "offered" else DERIVABLE.get(quantity, ())
        return how is not None and all(
            self._series.freshness(source.series(q))[0] == "fresh" for q in needed
        )

    # --- forecasts -------------------------------------------------------------------------

    async def spans(
        self, source: Source, quantity: str, start: datetime, end: datetime
    ) -> list[Span] | None:
        """A provider's forecast of a quantity, given or derived; None where it can't."""
        how = self.has(source, quantity)
        if how == "offered":
            return _spans(
                await self._series.get(source.series(quantity), start.timestamp(), end.timestamp())
            )
        if how != "derived":
            return None
        inputs = {
            q: _spans(await self._series.get(source.series(q), start.timestamp(), end.timestamp()))
            for q in DERIVABLE[quantity]
        }
        if quantity == "irradiance.global":
            location = await self._db.get(Location)
            if location is None:
                return None
            return [
                Span(
                    s.start,
                    s.end,
                    round(
                        sunlight(s.start, s.end, s.value, location.latitude, location.longitude), 1
                    ),
                )
                for s in inputs["cloud_cover"]
            ]
        other = {s.start: s.value for s in inputs[DERIVABLE[quantity][1]]}
        compute = dew_point if quantity == "dew_point" else relative_humidity
        return [
            Span(s.start, s.end, round(compute(s.value, other[s.start]), 2))
            for s in inputs["temperature"]
            if s.start in other
        ]

    async def forecast(self, quantity: str, start: datetime, end: datetime) -> Forecast:
        """A quantity's forecast from the first provider in the chosen order that has a
        fresh one; failing that, the first that has any, marked stale."""
        sources = self.sources()
        order = self.order(await self.choice(), quantity)
        stale: Forecast | None = None
        for key in order:
            source = sources.get(key)
            spans = await self.spans(source, quantity, start, end) if source else None
            if source is None or not spans:
                continue
            found = Forecast(
                quantity,
                key,
                derived=self.has(source, quantity) == "derived",
                fallback=key != order[0],
                spans=spans,
            )
            if self.fresh(source, quantity):
                return found
            if stale is None:
                found.stale = True
                stale = found
        return stale or Forecast(quantity, None)

    # --- scoring ---------------------------------------------------------------------------

    async def snapshot(self) -> int:
        """Keep each provider's forecasts for the hours LEADS_H ahead. Returns how many."""
        now = datetime.fromtimestamp(self._clock(), UTC)
        rows = []
        for key, source in self.sources().items():
            for quantity in SCORED:
                if not self.fresh(source, quantity):
                    continue
                for lead in LEADS_H:
                    valid = next_hour(now + timedelta(hours=lead))
                    spans = await self.spans(
                        source, quantity, valid - timedelta(hours=7), valid + timedelta(hours=7)
                    )
                    value = value_at(spans or [], valid, instant=True)
                    if value is not None:
                        rows.append(
                            (key, quantity, valid.timestamp(), lead, value, now.timestamp())
                        )
        if rows:

            def write(t: Transaction) -> None:
                t.executemany(
                    "INSERT INTO forecast_leads (source, quantity, valid, lead, value, made)"
                    " VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT (source, quantity, valid, lead)"
                    " DO UPDATE SET value = excluded.value, made = excluded.made"
                    " WHERE forecast_leads.observed IS NULL",
                    rows,
                )

            await self._db.run(write)
        return len(rows)

    async def observe(self) -> int:
        """Keep what the outdoor reference measured at each hour a kept forecast was for,
        once the hour has come. Returns how many were found."""
        now = self._clock()

        def waiting(t: Transaction) -> list[tuple[str, float]]:
            return list(
                t.execute(
                    "SELECT DISTINCT quantity, valid FROM forecast_leads"
                    " WHERE observed IS NULL AND valid <= ? AND valid > ?",
                    (now, now - 2 * 86_400),
                )
            )

        found = []
        for quantity, valid in await self._db.run(waiting):
            samples = await self._values.history(
                PointKey(SITE, f"outdoor/{quantity}"), valid - OBSERVED_WITHIN_S, valid + 1
            )
            # The value at the hour is the last one kept before it, if that was good: the
            # history keeps a value when it changes, and a sensor gone quiet turns stale.
            if samples and samples[-1].quality == "good" and samples[-1].value is not None:
                found.append((samples[-1].value, quantity, valid))
        if found:

            def write(t: Transaction) -> None:
                t.executemany(
                    "UPDATE forecast_leads SET observed = ? WHERE quantity = ? AND valid = ?"
                    " AND observed IS NULL",
                    found,
                )

            await self._db.run(write)
        return len(found)

    async def scores(self, days: int = WINDOW_DAYS) -> list[Score]:
        """Bias (forecast less measured) and mean absolute error, by provider, quantity
        and lead time, over the last `days`."""
        since = self._clock() - days * 86_400

        def read(t: Transaction) -> list[tuple[str, str, int, int, float, float]]:
            return list(
                t.execute(
                    "SELECT source, quantity, lead, COUNT(*), AVG(value - observed),"
                    " AVG(ABS(value - observed)) FROM forecast_leads"
                    " WHERE observed IS NOT NULL AND valid >= ?"
                    " GROUP BY source, quantity, lead ORDER BY source, quantity, lead",
                    (since,),
                )
            )

        return [
            Score(source, quantity, lead, n, round(bias, 2), round(mae, 2))
            for source, quantity, lead, n, bias, mae in await self._db.run(read)
        ]

    async def prune(self) -> None:
        now = self._clock()

        def delete(t: Transaction) -> None:
            t.execute(
                "DELETE FROM forecast_leads WHERE valid < ?", (now - KEEP_LEADS_DAYS * 86_400,)
            )
            t.execute(
                "DELETE FROM intervals WHERE status = 'forecast' AND end < ?",
                (now - KEEP_FORECASTS_DAYS * 86_400,),
            )

        await self._db.run(delete)

    async def run(self, every_s: float = 300.0, prune_s: float = 3600.0) -> None:
        """Keep forecasts and measurements, and prune, until cancelled."""
        last_prune = 0.0
        while True:
            await asyncio.sleep(every_s)
            try:
                await self.snapshot()
                await self.observe()
                if time.monotonic() - last_prune >= prune_s:
                    await self.prune()
                    last_prune = time.monotonic()
            except Exception:
                log.exception("keeping forecasts for scoring failed; trying again later")


def _spans(intervals: Iterable[Interval]) -> list[Span]:
    return [Span(i.start, i.end, i.value) for i in intervals if math.isfinite(i.value)]
