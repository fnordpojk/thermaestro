"""The MET Norway plugin: Locationforecast 2.0 (`complete`) for the location.

What the answer holds, as seen in it:
- a time series with hourly steps to about two days ahead, then six-hourly, to about
  ten days;
- at each time, values for that instant (temperature with its 10th and 90th percentiles
  in the Nordic area, dew point, humidity, cloud cover, wind and gusts, pressure at sea
  level), and amounts for the next 1, 6 and 12 hours: precipitation is taken for the next
  hour where there is one, and for the next six hours after that;
- its units in `meta.units`, checked against the ones expected;
- no sunlight: irradiance has to come from elsewhere, or from cloud cover.

The terms: an identifying User-Agent (this plugin's name, and where to read about it), no
request before the answer's `Expires`, then `If-Modified-Since`; a 203 means the version
is deprecated, and a 403 that the requests are refused.
"""

import json
import logging
from datetime import datetime, timedelta
from typing import Any

import aiohttp

from ..cap.model import Access, ForecastModel, Interval, Knowledge, Provider, Terms
from ..core.plugins import PluginContext
from ..forecast import Fetched, ForecastPlugin, http_date, instants, interval
from ..seriesplugin import Refused, SourceError
from ..store import WeatherPoint

log = logging.getLogger(__name__)

URL = "https://api.met.no/weatherapi/locationforecast/2.0/complete"
DOCS = "MET Norway's Locationforecast 2.0 documentation, data model and terms, read 2026-10-06"

INSTANT = {
    "temperature": "air_temperature",
    "temperature.p10": "air_temperature_percentile_10",
    "temperature.p90": "air_temperature_percentile_90",
    "dew_point": "dew_point_temperature",
    "relative_humidity": "relative_humidity",
    "cloud_cover": "cloud_area_fraction",
    "wind_speed": "wind_speed",
    "wind_gust": "wind_speed_of_gust",
    "wind_direction": "wind_from_direction",
    "pressure": "air_pressure_at_sea_level",
}
UNITS = {
    "air_temperature": "celsius",
    "dew_point_temperature": "celsius",
    "relative_humidity": "%",
    "cloud_area_fraction": "%",
    "wind_speed": "m/s",
    "wind_from_direction": "degrees",
    "air_pressure_at_sea_level": "hPa",
    "precipitation_amount": "mm",
}
"""The units the plugin expects, by MET Norway's name; any other is refused."""


class MetNorwayPlugin(ForecastPlugin):
    name = "met_norway"
    version = "0.1.0"
    root = "met_norway"
    label = "MET Norway"

    def __init__(self, settings: WeatherPoint, *, url: str = URL, **kwargs: Any) -> None:
        super().__init__(settings.latitude, settings.longitude, **kwargs)
        self._url = url
        self._last_modified: str | None = None
        self._deprecated_said = False

    def models(self) -> Knowledge[tuple[ForecastModel, ...]]:
        return Knowledge(
            value=(
                ForecastModel(
                    name="MEPS, post-processed to 1 km with citizen stations",
                    grid_km=2.5,
                    area="the Nordic countries, to about 2.5 days",
                ),
                ForecastModel(
                    name="ECMWF ensemble", grid_km=9, area="the Nordic countries, beyond"
                ),
                ForecastModel(
                    name="ECMWF high resolution", grid_km=9, area="the rest of the world"
                ),
            ),
            known="documented",
            basis=DOCS,
        )

    def updates(self) -> Knowledge[str]:
        return Knowledge(value="PT1H", known="documented", basis=f"{DOCS}: MEPS runs every hour")

    def provider(self) -> Provider:
        return Provider(
            name="MET Norway",
            operator=Knowledge(
                value="the Norwegian Meteorological Institute", known="documented", basis=DOCS
            ),
            coverage=Knowledge(
                value=(
                    "the whole world; in the Nordic countries post-processed to 1 km, with"
                    " percentiles and gusts, which elsewhere it doesn't give"
                ),
                known="documented",
                basis=DOCS,
            ),
            access=Access(
                key=Knowledge(value=False, known="documented", basis=DOCS),
                rate_limit=Knowledge(
                    value="20 requests a second per application, all its users together",
                    known="documented",
                    basis=DOCS,
                ),
                requirements=Knowledge(
                    value=(
                        "an identifying User-Agent with a contact",
                        "no request before the answer expires, then If-Modified-Since",
                        "at most four decimals in the coordinates",
                    ),
                    known="documented",
                    basis=DOCS,
                ),
            ),
            terms=Terms(
                license=Knowledge(value="CC BY 4.0 and NLOD 2.0", known="documented", basis=DOCS),
                attribution=Knowledge(value="Data from MET Norway", known="documented", basis=DOCS),
                storing_allowed=Knowledge(
                    value=True, known="documented", basis=f"{DOCS}: CC BY 4.0"
                ),
            ),
            verification=Knowledge(
                value=(
                    "quarterly verification reports (MET-info), with errors by lead time for"
                    " the post-processed temperature and wind"
                ),
                known="documented",
                basis="MET-info 27/2026",
            ),
        )

    async def fetch(self, session: aiohttp.ClientSession) -> Fetched:
        params = {"lat": str(self.latitude), "lon": str(self.longitude)}
        headers = {}
        if self._last_modified and self._infos:
            headers["If-Modified-Since"] = self._last_modified
        try:
            async with session.get(self._url, params=params, headers=headers) as answer:
                status, body = answer.status, await answer.read()
                expires = http_date(answer.headers.get("Expires"))
                last_modified = answer.headers.get("Last-Modified")
        except aiohttp.ClientError as e:
            raise SourceError(f"MET Norway can't be reached ({type(e).__name__})") from None
        if status == 304:
            return Fetched(None, expires=expires)
        if status == 403:
            raise Refused(
                "MET Norway refuses Thermaestro's requests (HTTP 403); a newer Thermaestro"
                " may be needed"
            )
        if status == 429:
            raise SourceError("MET Norway: too many requests")
        if status == 203 and not self._deprecated_said:
            log.warning("MET Norway says this version of its API is deprecated")
            self._deprecated_said = True
        if status not in (200, 203):
            raise SourceError(f"MET Norway answered HTTP {status}")
        try:
            document = json.loads(body)
        except ValueError:
            raise SourceError("MET Norway's answer isn't JSON") from None
        intervals, issued = parse(document)
        self._last_modified = last_modified
        return Fetched(intervals, issued=issued, expires=expires)


def parse(document: dict[str, Any]) -> tuple[list[Interval], datetime | None]:
    """The intervals in an answer, and when its forecast was made."""
    try:
        properties = document["properties"]
        meta = properties["meta"]
        series = properties["timeseries"]
        issued = datetime.fromisoformat(meta["updated_at"]) if meta.get("updated_at") else None
        units = meta.get("units", {})
        times = [datetime.fromisoformat(t["time"]) for t in series]
        details = [t["data"]["instant"]["details"] for t in series]
    except (KeyError, TypeError, ValueError) as e:
        raise SourceError(f"MET Norway's answer can't be read ({type(e).__name__}: {e})") from None
    for name, expected in UNITS.items():
        if name in units and units[name] != expected:
            raise SourceError(f"MET Norway gives {name} in {units[name]!r}, not {expected!r}")
    out: list[Interval] = []
    for quantity, name in INSTANT.items():
        values = [_number(d.get(name)) for d in details]
        if any(v is not None for v in values):
            out += instants(quantity, times, values, issued)
    covered: datetime | None = None
    for start, entry in zip(times, series, strict=True):
        data = entry["data"]
        for block, hours in (("next_1_hours", 1), ("next_6_hours", 6)):
            amount = _number(data.get(block, {}).get("details", {}).get("precipitation_amount"))
            if amount is None:
                continue
            if covered is None or start >= covered:
                end = start + timedelta(hours=hours)
                out.append(interval("precipitation", start, end, amount, issued))
                covered = end
            break
    return out, issued


def _number(raw: Any) -> float | None:
    return float(raw) if isinstance(raw, int | float) and not isinstance(raw, bool) else None


def create(context: PluginContext) -> MetNorwayPlugin:
    """The entry point: a plugin instance from its settings."""
    return MetNorwayPlugin(WeatherPoint.model_validate(dict(context.settings)))
