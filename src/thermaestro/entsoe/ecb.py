"""The ECB's euro reference rates, for prices published in euros and used elsewhere.

The rates come out around 16:00 CET each working day, after the day-ahead auction at
noon. A delivery day's prices are converted at the latest rate dated two days or more
before it: the latest one out when that day's auction closed. So a day's prices never
change once converted, whenever they are fetched.
"""

import xml.etree.ElementTree as ET
from datetime import date, timedelta

import aiohttp

from ..dayahead import SourceError

URL = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-hist-90d.xml"
SOURCE = "ECB euro foreign exchange reference rates"


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
