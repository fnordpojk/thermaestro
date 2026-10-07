"""The SMHI plugin: the snow1g point forecast (version 1) for the location.

What the answer holds, as seen in it:
- a time series with hourly steps to about 2.5 days ahead, then 2, 3, 6 and 12 hours, to
  about ten days;
- at each time, values for that instant (temperature, humidity, cloud cover in oktas,
  wind and gusts, pressure at sea level), and precipitation for the period from
  `intervalParametersStartTime` to the time;
- no dew point and no sunlight: the core derives the dew point from temperature and
  humidity, and sunlight from cloud cover;
- a missing value is 9999.

Cloud cover is turned from oktas (eighths of the sky) into percent. A place outside the
grid gets a 404, "Requested point is out of bounds": SMHI covers about 50 to 73° N and
0 to 54° E. The answer says how long to keep it (`Expires`); it isn't asked before.
"""

import json
from datetime import datetime
from typing import Any

import aiohttp

from ..cap.model import Access, ForecastModel, Interval, Knowledge, Provider, Terms
from ..core.plugins import PluginContext
from ..forecast import Fetched, ForecastPlugin, http_date, instants, interval
from ..seriesplugin import Refused, SourceError
from ..store import WeatherPoint

URL = "https://opendata-download-metfcst.smhi.se/api/category/snow1g/version/1/geotype/point"
DOCS = "SMHI's snow1g v1 documentation, catalog entry and conditions of use, read 2026-10-06"
MISSING = 9999

INSTANT = {
    "temperature": "air_temperature",
    "relative_humidity": "relative_humidity",
    "cloud_cover": "cloud_area_fraction",
    "wind_speed": "wind_speed",
    "wind_gust": "wind_speed_of_gust",
    "wind_direction": "wind_from_direction",
    "pressure": "air_pressure_at_mean_sea_level",
}


class SmhiPlugin(ForecastPlugin):
    name = "smhi"
    version = "0.1.0"
    root = "smhi"
    label = "SMHI"

    def __init__(self, settings: WeatherPoint, *, url: str = URL, **kwargs: Any) -> None:
        super().__init__(settings.latitude, settings.longitude, **kwargs)
        self._url = url

    def models(self) -> Knowledge[tuple[ForecastModel, ...]]:
        return Knowledge(
            value=(
                ForecastModel(
                    name="SMHI's SNOW database: several models, chosen by a meteorologist",
                    grid_km=2.5,
                ),
            ),
            known="documented",
            basis=f"{DOCS}; the models aren't named",
        )

    def updates(self) -> Knowledge[str]:
        return Knowledge(value="PT15M", known="documented", basis=f"{DOCS}: four times an hour")

    def provider(self) -> Provider:
        return Provider(
            name="SMHI",
            operator=Knowledge(
                value="the Swedish Meteorological and Hydrological Institute",
                known="documented",
                basis=DOCS,
            ),
            coverage=Knowledge(
                value="the Nordic and Baltic countries: 50.3 to 72.8° N, 0.3 to 54.2° E",
                known="documented",
                basis=DOCS,
            ),
            access=Access(
                key=Knowledge(value=False, known="documented", basis=DOCS),
                rate_limit=Knowledge(
                    value="none published; no unnecessary or repeated downloads",
                    known="documented",
                    basis=DOCS,
                ),
                requirements=Knowledge(
                    value=("keep an answer as long as it says", "the documented APIs only"),
                    known="documented",
                    basis=DOCS,
                ),
            ),
            terms=Terms(
                license=Knowledge(value="CC BY 4.0", known="documented", basis=DOCS),
                attribution=Knowledge(
                    value="Data from SMHI; cloud cover converted from oktas to percent",
                    known="documented",
                    basis=f"{DOCS}: credit SMHI and say what was changed",
                ),
                storing_allowed=Knowledge(
                    value=True, known="documented", basis=f"{DOCS}: CC BY 4.0"
                ),
            ),
            verification=Knowledge(
                value=(
                    "the method is published (about 180 stations; a temperature within 2 °C"
                    " counts as right); the figures aren't on the API's pages"
                ),
                known="documented",
                basis=DOCS,
            ),
        )

    async def fetch(self, session: aiohttp.ClientSession) -> Fetched:
        url = f"{self._url}/lon/{self.longitude}/lat/{self.latitude}/data.json"
        try:
            async with session.get(url) as answer:
                status, body = answer.status, await answer.read()
                expires = http_date(answer.headers.get("Expires"))
        except aiohttp.ClientError as e:
            raise SourceError(f"SMHI can't be reached ({type(e).__name__})") from None
        if status == 404 and b"out of bounds" in body:
            raise Refused(
                "the location is outside SMHI's forecast area (the Nordic and Baltic countries)"
            )
        if status != 200:
            raise SourceError(f"SMHI answered HTTP {status}")
        try:
            document = json.loads(body)
        except ValueError:
            raise SourceError("SMHI's answer isn't JSON") from None
        intervals, issued = parse(document)
        return Fetched(intervals, issued=issued, expires=expires)


def parse(document: dict[str, Any]) -> tuple[list[Interval], datetime | None]:
    """The intervals in an answer, and when its forecast was made."""
    try:
        issued = datetime.fromisoformat(document["createdTime"])
        series = document["timeSeries"]
        times = [datetime.fromisoformat(t["time"]) for t in series]
        data = [t["data"] for t in series]
        periods = [
            datetime.fromisoformat(t["intervalParametersStartTime"])
            if t.get("intervalParametersStartTime")
            else None
            for t in series
        ]
    except (KeyError, TypeError, ValueError) as e:
        raise SourceError(f"SMHI's answer can't be read ({type(e).__name__}: {e})") from None
    out: list[Interval] = []
    for quantity, name in INSTANT.items():
        values = [_number(d.get(name)) for d in data]
        if quantity == "cloud_cover":
            values = [None if v is None else min(v, 8) * 12.5 for v in values]
        if any(v is not None for v in values):
            out += instants(quantity, times, values, issued)
    for end, start, d in zip(times, periods, data, strict=True):
        amount = _number(d.get("precipitation_amount_mean"))
        if start is not None and amount is not None and start < end:
            out.append(interval("precipitation", start, end, amount, issued))
    return out, issued


def _number(raw: Any) -> float | None:
    if isinstance(raw, bool) or not isinstance(raw, int | float) or raw == MISSING:
        return None
    return float(raw)


def create(context: PluginContext) -> SmhiPlugin:
    """The entry point: a plugin instance from its settings."""
    return SmhiPlugin(WeatherPoint.model_validate(dict(context.settings)))
