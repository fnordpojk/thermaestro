"""The Energy-Charts plugin: a bidding zone's day-ahead prices from Fraunhofer ISE's
Energy-Charts API, which needs no account.

Asks `/price` for the zone's today and tomorrow; the API reads a date in the zone's own
time zone. An answer gives each price's start (`unix_seconds`) and the price in EUR per
MWh. A price lasts until the next one starts, and the last one as long as the step
between the others: 15 minutes, or an hour in Switzerland, whose price is hourly.

The license is per zone, and comes with every answer (`license_info`). The zones whose
prices Energy-Charts takes from the German regulator's SMARD are CC BY 4.0; the others
are "for private and internal use only". The plugin passes the license on as the answer
gives it, and before the first answer, as the API's documentation lists the zones.

The API takes two price requests a minute from one address; a fetch is one request.

Checked against the real API on 2026-10-08 (NO1, DE-LU, CH, SE3, FI, IT and others).
"""

import logging
from datetime import UTC, date, datetime, timedelta
from itertools import pairwise
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

log = logging.getLogger(__name__)

URL = "https://api.energy-charts.info/price"
DOCS = "Energy-Charts API documentation (api.energy-charts.info, openapi.json), read 2026-10-08"
NAMES = {
    "IT-CALA": "IT-Calabria",
    "IT-CNOR": "IT-Centre-North",
    "IT-CSUD": "IT-Centre-South",
    "IT-NORD": "IT-North",
    "IT-SARD": "IT-Sardinia",
    "IT-SICI": "IT-Sicily",
    "IT-SUD": "IT-South",
}
"""The zones Energy-Charts names otherwise than ENTSO-E does."""
CC_BY = frozenset(
    {"AT", "BE", "CH", "CZ", "DE-LU", "DK1", "DK2", "FR", "HU", "IT-NORD", "NL", "NO2"}
    | {"PL", "SE4", "SI"}
)
"""The zones the documentation lists as CC BY 4.0, from Bundesnetzagentur | SMARD.de."""
CC_BY_LICENSE = "CC BY 4.0 (creativecommons.org/licenses/by/4.0) from Bundesnetzagentur | SMARD.de"
PRIVATE_LICENSE = (
    "The data provided herein is for private and internal use only. The utilization of any"
    " data, whether in its raw or derived form, for external or commercial purposes is"
    " expressly prohibited. Should you require licensing for market-related data, please"
    " direct your inquiries to the original data providers, including but not limited to"
    " EPEX SPOT SE."
)
PRIVATE = "private and internal use only"
HOURLY = frozenset({"CH"})


class EnergyChartsPlugin(DayAheadPlugin):
    name = "energy_charts"
    version = "0.1.0"
    root = "energy_charts"
    label = "Energy-Charts"

    def __init__(
        self,
        settings: SpotZone,
        *,
        url: str = URL,
        rates: ecb.Rates | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        if settings.zone not in ZONES:
            raise ValueError(f"no bidding zone {settings.zone!r}")
        self.settings = settings
        self.area = ZONES[settings.zone]
        self._url = url
        self.converter = ecb.Converter(rates)
        self.license: str | None = None
        """As the last answer gave it."""

    # --- what Energy-Charts supplies -------------------------------------------------------

    def zone(self) -> ZoneInfo:
        return ZoneInfo(self.area.tz)

    def publication(self) -> Publication:
        # No time is stated; the auction's results come out at about 12:57 CET.
        return Publication(daily_after="13:00", tz="Europe/Brussels")

    def series_infos(self) -> tuple[SeriesInfo, ...]:
        return (
            SeriesInfo(
                id="spot",
                kind="price",
                role="energy.spot",
                covers=Knowledge(value=(), known="documented", basis=DOCS),
                unit=f"{self.settings.currency}/kWh",
                vat="excl",
                resolution="PT1H" if self.settings.zone in HOURLY else "PT15M",
                area=self.settings.zone,
                publication=self.publication(),
            ),
        )

    def provider(self) -> Provider:
        license = self.license or (
            CC_BY_LICENSE if self.settings.zone in CC_BY else PRIVATE_LICENSE
        )
        known = "verified" if self.license else "documented"
        basis = f"the answer for {self.settings.zone}" if self.license else DOCS
        private = PRIVATE in license
        attribution = (
            "Day-ahead prices: Energy-Charts (Fraunhofer ISE)"
            if private
            else "Day-ahead prices: Bundesnetzagentur | SMARD.de, through Energy-Charts"
        )
        conditions = []
        if private:
            conditions.append(
                "For private and internal use only: not for external or commercial purposes"
            )
        if self.settings.currency != "EUR":
            attribution += f"; converted to {self.settings.currency} at the {ecb.SOURCE}"
            conditions.append(ecb.CONDITION)
        return Provider(
            name="Energy-Charts",
            operator=Knowledge(
                value="Fraunhofer Institute for Solar Energy Systems ISE",
                known="documented",
                basis=DOCS,
            ),
            coverage=Knowledge(
                value="the bidding zones of the European day-ahead market, and some others",
                known="documented",
                basis=DOCS,
            ),
            access=Access(
                key=Knowledge(value=False, known="documented", basis=DOCS),
                rate_limit=Knowledge(
                    value="2 price requests a minute per address", known="documented", basis=DOCS
                ),
            ),
            terms=Terms(
                license=Knowledge(value=license, known=known, basis=basis),
                attribution=Knowledge(value=attribution, known=known, basis=basis),
                conditions=Knowledge(value=tuple(conditions), known=known, basis=basis),
            ),
        )

    async def fetch(self, session: aiohttp.ClientSession, days: list[date]) -> list[Interval]:
        params = {
            "bzn": NAMES.get(self.settings.zone, self.settings.zone),
            "start": min(days).isoformat(),
            "end": max(days).isoformat(),
        }
        async with session.get(self._url, params=params) as answer:
            if answer.status == 404:
                return []  # nothing published for these days
            if answer.status == 429:
                raise SourceError("Energy-Charts: too many requests")
            if answer.status != 200:
                raise SourceError(f"Energy-Charts answered HTTP {answer.status}")
            try:
                data = await answer.json(content_type=None)
            except ValueError:
                raise SourceError("Energy-Charts' answer isn't JSON") from None
        return await self.converter.intervals(
            session, self._rows(data), self.settings.currency, self.zone()
        )

    def _rows(self, data: object) -> list[tuple[datetime, datetime, float]]:
        if not isinstance(data, dict):
            raise SourceError("Energy-Charts' answer isn't an object")
        if data.get("unit") not in (None, "EUR / MWh"):
            raise SourceError(f"prices in {data.get('unit')!r}, not EUR / MWh")
        if isinstance(data.get("license_info"), str):
            self.license = data["license_info"]
        if data.get("deprecated"):
            log.warning("Energy-Charts marks its price API as deprecated")
        try:
            starts = [datetime.fromtimestamp(int(t), UTC) for t in data.get("unix_seconds") or []]
            prices = list(data.get("price") or [])
        except (TypeError, ValueError):
            raise SourceError("a time Energy-Charts sent can't be read") from None
        if len(starts) != len(prices):
            raise SourceError("Energy-Charts sent more times than prices, or fewer")
        steps = [b - a for a, b in pairwise(starts) if b > a]
        step = min(steps) if steps else timedelta(minutes=15)
        out = []
        for n, (start, price) in enumerate(zip(starts, prices, strict=True)):
            if price is None:
                continue  # not published
            end = min(starts[n + 1], start + step) if n + 1 < len(starts) else start + step
            try:
                out.append((start, end, float(price)))
            except (TypeError, ValueError):
                raise SourceError(f"a price Energy-Charts sent can't be read: {price!r}") from None
        return out


def create(context: PluginContext) -> EnergyChartsPlugin:
    """The entry point: a plugin instance from its settings."""
    return EnergyChartsPlugin(SpotZone.model_validate(dict(context.settings)))
