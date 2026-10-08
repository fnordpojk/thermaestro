"""The Nordic price sites' plugin: day-ahead prices from elprisetjustnu.se (Sweden),
hvakosterstrommen.no (Norway), elprisenligenu.dk (Denmark) and sahkonhintatanaan.fi
(Finland). One company, Beneficial Apps AS, runs all four, with one API and no account.

Each day of a zone is a static file of that local day's prices: per 15 minutes in
Sweden, per hour elsewhere, each hour the mean of its four quarters. A day not yet
published is a 404. The plugin takes the price in euros (`EUR_per_kWh`, the exchange's
own number to the site's rounding) and converts it at the ECB's rates, as every other
source here does, rather than at the site's own rate.

The sites say they take the prices from ENTSO-E, and grant their use "to anyone, for
anything"; they ask to be cited as the source where the prices are shown publicly.

Checked against the real sites on 2026-10-08: every zone, both days, against Energi Data
Service, Elering and Energy-Charts, with no difference beyond rounding.
"""

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

import aiohttp

from .. import ecb
from ..cap.model import (
    Access,
    Interval,
    Knowledge,
    Provider,
    Publication,
    SeriesInfo,
    Terms,
)
from ..core.plugins import PluginContext
from ..dayahead import DayAheadPlugin, SourceError
from ..store import SpotZone
from ..zones import ZONES


@dataclass(frozen=True, slots=True)
class Site:
    host: str
    page: str
    """The site's page about its API, where its terms are."""
    resolution: str
    terms: str
    """Its grant, as its API page words it."""
    zoned: bool = True
    """Whether a file's name has the zone: Finland is one zone, and its don't."""


SITES = {
    "SE": Site(
        "www.elprisetjustnu.se",
        "https://www.elprisetjustnu.se/elpris-api",
        "PT15M",
        "Fritt tillgänglig för vem som helst, för vad som helst.",
    ),
    "NO": Site(
        "www.hvakosterstrommen.no",
        "https://www.hvakosterstrommen.no/strompris-api",
        "PT1H",
        "Fritt tilgjengelig for hvem som helst, til hva som helst.",
    ),
    "DK": Site(
        "www.elprisenligenu.dk",
        "https://www.elprisenligenu.dk/elpris-api",
        "PT1H",
        "Frit tilgængelig for enhver, for hvad som helst.",
    ),
    "FI": Site(
        "www.sahkonhintatanaan.fi",
        "https://www.sahkonhintatanaan.fi/sahkon-hinta-api",
        "PT1H",
        "Vapaasti kaikkien saatavilla, mihin tahansa.",
        zoned=False,
    ),
}
READ = "read 2026-10-08"


class NordicSitesPlugin(DayAheadPlugin):
    name = "nordic_sites"
    version = "0.1.0"
    root = "nordic_sites"

    def __init__(
        self,
        settings: SpotZone,
        *,
        base: str | None = None,
        rates: ecb.Rates | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        area = ZONES.get(settings.zone)
        if area is None or area.country not in SITES:
            raise ValueError(f"no Nordic price site for {settings.zone!r}")
        self.settings = settings
        self.area = area
        self.site = SITES[area.country]
        self.label = self.site.host.removeprefix("www.")
        self._base = base or f"https://{self.site.host}"
        self.converter = ecb.Converter(rates)

    # --- what the site supplies ------------------------------------------------------------

    def zone(self) -> ZoneInfo:
        return ZoneInfo(self.area.tz)

    def publication(self) -> Publication:
        # "Tomorrow's price arrives at 13:00 the day before at the earliest" (14:00 in Finland).
        return Publication(daily_after="13:00", tz="Europe/Brussels")

    def series_infos(self) -> tuple[SeriesInfo, ...]:
        docs = f"{self.site.page}, {READ}"
        return (
            SeriesInfo(
                id="spot",
                kind="price",
                role="energy.spot",
                covers=Knowledge(value=(), known="documented", basis=docs),
                unit=f"{self.settings.currency}/kWh",
                vat="excl",
                resolution=self.site.resolution,
                area=self.settings.zone,
                publication=self.publication(),
            ),
        )

    def provider(self) -> Provider:
        docs = f"{self.site.page}, {READ}"
        attribution = f"Day-ahead prices: {self.label}"
        conditions = [
            "The site takes the prices from ENTSO-E; its grant is its own, and says"
            " nothing of ENTSO-E's terms or the power exchanges' rights",
        ]
        if self.settings.currency != "EUR":
            attribution += f"; converted to {self.settings.currency} at the {ecb.SOURCE}"
            conditions.append(ecb.CONDITION)
        return Provider(
            name=self.label,
            operator=Knowledge(value="Beneficial Apps AS", known="documented", basis=docs),
            coverage=Knowledge(
                value=f"the bidding zones of {self.area.country}", known="documented", basis=docs
            ),
            access=Access(key=Knowledge(value=False, known="documented", basis=docs)),
            terms=Terms(
                license=Knowledge(
                    value=f'"{self.site.terms}" (free for anyone, for anything)',
                    known="documented",
                    basis=docs,
                ),
                attribution=Knowledge(value=attribution, known="documented", basis=docs),
                conditions=Knowledge(value=tuple(conditions), known="documented", basis=docs),
            ),
        )

    async def fetch(self, session: aiohttp.ClientSession, days: list[date]) -> list[Interval]:
        rows: list[tuple[datetime, datetime, float]] = []
        for day in days:
            rows += await self._day(session, day)
        return await self.converter.intervals(session, rows, self.settings.currency, self.zone())

    async def _day(
        self, session: aiohttp.ClientSession, day: date
    ) -> list[tuple[datetime, datetime, float]]:
        name = f"{day:%m-%d}_{self.settings.zone}" if self.site.zoned else f"{day:%m-%d}"
        url = f"{self._base}/api/v1/prices/{day:%Y}/{name}.json"
        async with session.get(url) as answer:
            if answer.status == 404:
                return []  # not published yet
            if answer.status != 200:
                raise SourceError(f"{self.label} answered HTTP {answer.status}")
            try:
                data = await answer.json(content_type=None)
            except ValueError:
                raise SourceError(f"{self.label}'s answer isn't JSON") from None
        if not isinstance(data, list):
            raise SourceError(f"{self.label}'s answer isn't a list of prices")
        out = []
        for price in data:
            try:
                start = datetime.fromisoformat(price["time_start"])
                end = datetime.fromisoformat(price["time_end"])
                out.append((start, end, float(price["EUR_per_kWh"]) * 1000))
            except (KeyError, TypeError, ValueError) as e:
                raise SourceError(f"a price {self.label} sent can't be read ({e})") from None
        return out


def create(context: PluginContext) -> NordicSitesPlugin:
    """The entry point: a plugin instance from its settings."""
    return NordicSitesPlugin(SpotZone.model_validate(dict(context.settings)))
