"""The Octopus Agile plugin: what Octopus Energy's Agile tariff charges per kWh in a region
of Great Britain, per half hour, from Octopus' public API with no account.

Great Britain left the European day-ahead market in 2021, and no source gives its
auction's price without an account. Agile is a household tariff that follows the
wholesale price, so for a household on Agile its unit rate is the price to plan with.

The current Agile product is found among Octopus' products (its code changes with each
version, AGILE-24-10-01 when written); its unit rates come per half hour in pence per
kWh, with and without VAT. Octopus publishes the next day's "between 4-8pm every day
(usually nearer 4pm)", up to 23:00 the next evening. The unit rate is "capped at 100p/kWh
(including VAT)". What the rate holds besides the wholesale price, network charges
among them, isn't stated; Octopus says the standing charge covers "metering,
distribution, and other fixed costs".

Checked against the real API on 2026-10-08 (region C).
"""

from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import aiohttp

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
from ..store import OctopusAgile

URL = "https://api.octopus.energy/v1"
DOCS = "Octopus Energy API documentation (docs.octopus.energy), read 2026-10-08"
PRODUCT = "Octopus' Agile product description, read 2026-10-08"
LONDON = ZoneInfo("Europe/London")
DAY_STARTS = 23
"""Agile's prices run from 23:00 to 23:00."""
REGIONS = {
    "A": "Eastern England",
    "B": "East Midlands",
    "C": "London",
    "D": "Merseyside & North Wales",
    "E": "West Midlands",
    "F": "North East England",
    "G": "North West England",
    "H": "Southern England",
    "J": "South Eastern England",
    "K": "South Wales",
    "L": "South Western England",
    "M": "Yorkshire",
    "N": "Southern Scotland",
    "P": "Northern Scotland",
}
"""The regions by the letter that ends a tariff's code (E-1R-AGILE-24-10-01-C is London),
as energy-stats.uk's table of the distribution network regions names them (2026-10-08)."""
PAGES = 10
"""At most this many pages of products are read; there were 31 products, on one page."""


class OctopusAgilePlugin(DayAheadPlugin):
    name = "octopus_agile"
    version = "0.1.0"
    root = "octopus_agile"
    label = "Octopus Agile"

    def __init__(self, settings: OctopusAgile, *, url: str = URL, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.settings = settings
        self._url = url
        self.product: str | None = None

    # --- what Octopus supplies --------------------------------------------------------------

    def zone(self) -> ZoneInfo:
        return LONDON

    def publication(self) -> Publication:
        return Publication(daily_after="16:00", tz="Europe/London")

    def market_end(self, today: date) -> datetime:
        return datetime.combine(today + timedelta(days=1), datetime.min.time(), LONDON).replace(
            hour=DAY_STARTS
        )

    def series_infos(self) -> tuple[SeriesInfo, ...]:
        common: dict[str, Any] = {
            "kind": "price",
            "role": "energy.supplier",
            "unit": "GBP/kWh",
            "resolution": "PT30M",
            "area": f"GB-{self.settings.region}",
            "publication": self.publication(),
        }
        basis = f"{PRODUCT}: half-hourly energy prices, tied to wholesale prices"
        return (
            SeriesInfo(
                id="unit_rate",
                covers=Knowledge(value=("energy.spot", "vat"), known="documented", basis=basis),
                vat="incl",
                **common,
            ),
            SeriesInfo(
                id="unit_rate.excl",
                covers=Knowledge(value=("energy.spot",), known="documented", basis=basis),
                vat="excl",
                **common,
            ),
        )

    def provider(self) -> Provider:
        return Provider(
            name="Octopus Energy",
            coverage=Knowledge(
                value="Great Britain, by region: what Agile customers pay",
                known="documented",
                basis=DOCS,
            ),
            access=Access(
                key=Knowledge(
                    value=False,
                    known="documented",
                    basis=f"{DOCS}: product endpoints don't require authentication",
                )
            ),
            terms=Terms(
                license=Knowledge(known="unknown", basis=f"{DOCS}: no license stated"),
                attribution=Knowledge(
                    value="Unit rates: Octopus Energy", known="documented", basis=DOCS
                ),
            ),
        )

    async def fetch(self, session: aiohttp.ClientSession, days: list[date]) -> list[Interval]:
        product = await self._product(session)
        tariff = f"E-1R-{product}-{self.settings.region}"
        start = datetime.combine(min(days), datetime.min.time(), LONDON).astimezone(UTC)
        end = datetime.combine(max(days) + timedelta(days=1), datetime.min.time(), LONDON)
        params = {
            "period_from": start.strftime("%Y-%m-%dT%H:%MZ"),
            "period_to": end.astimezone(UTC).strftime("%Y-%m-%dT%H:%MZ"),
            "page_size": "1500",
        }
        url = f"{self._url}/products/{product}/electricity-tariffs/{tariff}/standard-unit-rates/"
        data = await self._get(session, url, params)
        out = []
        for rate in data.get("results") or []:
            try:
                start_at = datetime.fromisoformat(rate["valid_from"])
                end_at = datetime.fromisoformat(rate["valid_to"])
                values = (
                    ("unit_rate", rate["value_inc_vat"], "incl"),
                    ("unit_rate.excl", rate["value_exc_vat"], "excl"),
                )
            except (KeyError, TypeError, ValueError) as e:
                raise SourceError(f"a unit rate Octopus sent can't be read ({e})") from None
            for series, pence, vat in values:
                out.append(
                    Interval.model_validate(
                        {
                            "series": series,
                            "start": start_at,
                            "end": end_at,
                            "value": round(float(pence) / 100, 6),
                            "unit": "GBP/kWh",
                            "vat": vat,
                            "status": "final",
                        }
                    )
                )
        return out

    async def _product(self, session: aiohttp.ClientSession) -> str:
        """The current Agile product's code, found once."""
        if self.product is None:
            products: list[Any] = []
            url: str | None = f"{self._url}/products/"
            params = {"brand": "OCTOPUS_ENERGY", "is_business": "false"}
            for _ in range(PAGES):
                if url is None:
                    break
                data = await self._get(session, url, params)
                products += data.get("results") or []
                url, params = data.get("next"), {}  # the next page's address holds the query
            agile = [
                p
                for p in products
                if isinstance(p, dict)
                and str(p.get("code", "")).startswith("AGILE-")
                and "OUTGOING" not in str(p.get("code"))
                and p.get("available_to") is None
            ]
            if not agile:
                raise SourceError("Octopus offers no Agile tariff now")
            self.product = str(max(agile, key=lambda p: str(p.get("available_from")))["code"])
        return self.product

    async def _get(
        self, session: aiohttp.ClientSession, url: str, params: dict[str, str]
    ) -> dict[str, Any]:
        async with session.get(url, params=params) as answer:
            if answer.status != 200:
                raise SourceError(f"Octopus answered HTTP {answer.status}")
            try:
                data = await answer.json(content_type=None)
            except ValueError:
                raise SourceError("Octopus' answer isn't JSON") from None
        if not isinstance(data, dict):
            raise SourceError("Octopus' answer isn't an object")
        return data


def create(context: PluginContext) -> OctopusAgilePlugin:
    """The entry point: a plugin instance from its settings."""
    return OctopusAgilePlugin(OctopusAgile.model_validate(dict(context.settings)))
