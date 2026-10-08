"""The ECB's euro reference rates, for prices published in euros and used elsewhere.

The rates come out around 16:00 CET each working day, after the day-ahead auction at
noon. A delivery day's prices are converted at the latest rate dated two days or more
before it: the latest one out when that day's auction closed. So a day's prices never
change once converted, whenever they are fetched, and every source converts them alike.
"""

import time
import xml.etree.ElementTree as ET
from collections.abc import Iterable
from datetime import date, datetime, timedelta, tzinfo
from typing import Any

import aiohttp

from .cap.model import Interval
from .seriesplugin import SourceError

URL = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-hist-90d.xml"
SOURCE = "ECB euro foreign exchange reference rates"
CONDITION = "The ECB's rates are for information only; a price converted with them must say so"
FRESH_S = 6 * 3600.0
"""How long loaded rates are used before they are loaded again for a day they lack."""


class Rates:
    def __init__(self, url: str = URL) -> None:
        self._url = url
        self.table: dict[date, dict[str, float]] = {}

    def for_day(self, day: date, currency: str) -> tuple[date, float] | None:
        """The rate a delivery day's prices are converted at, and the date it is of."""
        latest = day - timedelta(days=2)
        dated = [d for d in self.table if d <= latest and currency in self.table[d]]
        if not dated:
            return None
        chosen = max(dated)
        return chosen, self.table[chosen][currency]

    async def load(self, session: aiohttp.ClientSession) -> None:
        async with session.get(self._url) as answer:
            if answer.status != 200:
                raise SourceError(f"the ECB answered HTTP {answer.status}")
            self.table = _parse(await answer.read())


class Converter:
    """Euro prices per MWh as intervals per kWh in a currency, converted at the ECB's
    rates where the currency isn't the euro."""

    def __init__(self, rates: Rates | None = None) -> None:
        self.rates = rates or Rates()
        self._loaded: float | None = None

    async def intervals(
        self,
        session: aiohttp.ClientSession,
        rows: Iterable[tuple[datetime, datetime, float]],
        currency: str,
        zone: tzinfo,
        series: str = "spot",
    ) -> list[Interval]:
        """Each row's start, end and price in EUR per MWh, as an interval of `series`."""
        out = []
        for start, end, eur_mwh in rows:
            value = eur_mwh / 1000
            extra: dict[str, Any] = {}
            if currency != "EUR":
                day = start.astimezone(zone).date()
                rate = await self._rate(session, day, currency)
                value *= rate[1]
                extra = {
                    "source": "calculated",
                    "why": f"converted from EUR at the ECB's rate of {rate[0]}: {rate[1]}",
                }
            out.append(
                Interval.model_validate(
                    {
                        "series": series,
                        "start": start,
                        "end": end,
                        "value": round(value, 6),
                        "unit": f"{currency}/kWh",
                        "vat": "excl",
                        "status": "final",
                        **extra,
                    }
                )
            )
        return out

    async def _rate(
        self, session: aiohttp.ClientSession, day: date, currency: str
    ) -> tuple[date, float]:
        old = self._loaded is None or time.monotonic() - self._loaded > FRESH_S
        found = None if old else self.rates.for_day(day, currency)
        if found is None:
            try:
                await self.rates.load(session)
            except aiohttp.ClientError as e:
                raise SourceError(f"the ECB can't be reached ({type(e).__name__})") from None
            self._loaded = time.monotonic()
            found = self.rates.for_day(day, currency)
        if found is None:
            raise SourceError(f"the ECB has no {currency} rate for {day}'s prices")
        return found


def _parse(xml: bytes) -> dict[date, dict[str, float]]:
    # Python's expat refuses entity expansion bombs and ElementTree doesn't fetch
    # external entities, so this file needs no other XML parser.
    try:
        root = ET.fromstring(xml)  # noqa: S314
    except ET.ParseError as e:
        raise SourceError(f"the ECB's rates can't be read ({e})") from None
    out: dict[date, dict[str, float]] = {}
    for cube in root.iter():
        if _local(cube.tag) != "Cube" or "time" not in cube.attrib:
            continue
        try:
            day = date.fromisoformat(cube.attrib["time"])
            out[day] = {
                c.attrib["currency"]: float(c.attrib["rate"])
                for c in cube
                if "currency" in c.attrib
            }
        except (KeyError, ValueError) as e:
            raise SourceError(f"an ECB rate can't be read ({e})") from None
    return out


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]
