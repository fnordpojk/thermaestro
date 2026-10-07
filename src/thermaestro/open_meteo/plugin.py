"""The Open-Meteo plugin: its forecast API, three days ahead, hourly.

What the answer holds, as its documentation says and the answers show:
- values for an instant at each hour: temperature, dew point, humidity, cloud cover,
  wind and its direction, pressure at sea level;
- values for the hour before it: sunlight as the hour's mean (global, direct normal and
  diffuse), precipitation as the hour's sum, the gust as the hour's maximum;
- the units in `hourly_units`, asked for in m/s for wind and checked.

Without a model chosen it combines the most suitable ones for the place (`best_match`).
The answer says nothing of when its models ran or when to ask again, so it is asked every
hour. Its free use is for non-commercial purposes, which its terms say includes personal
home automation.
"""

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import aiohttp

from ..cap.model import Access, ForecastModel, Interval, Knowledge, Provider, Terms
from ..core.plugins import PluginContext
from ..forecast import Fetched, ForecastPlugin, instants, interval
from ..seriesplugin import Refused, SourceError
from ..store import OpenMeteo

URL = "https://api.open-meteo.com/v1/forecast"
DOCS = "Open-Meteo's API documentation, terms and license page, read 2026-10-06"
DAYS = 3

INSTANT = {
    "temperature": "temperature_2m",
    "dew_point": "dew_point_2m",
    "relative_humidity": "relative_humidity_2m",
    "cloud_cover": "cloud_cover",
    "wind_speed": "wind_speed_10m",
    "wind_direction": "wind_direction_10m",
    "pressure": "pressure_msl",
}
PRECEDING_HOUR = {
    "irradiance.global": "shortwave_radiation",
    "irradiance.direct_normal": "direct_normal_irradiance",
    "irradiance.diffuse": "diffuse_radiation",
    "wind_gust": "wind_gusts_10m",
    "precipitation": "precipitation",
}
UNITS = {
    "temperature_2m": "\N{DEGREE SIGN}C",
    "dew_point_2m": "\N{DEGREE SIGN}C",
    "relative_humidity_2m": "%",
    "cloud_cover": "%",
    "wind_speed_10m": "m/s",
    "wind_gusts_10m": "m/s",
    "wind_direction_10m": "\N{DEGREE SIGN}",
    "pressure_msl": "hPa",
    "shortwave_radiation": "W/m\N{SUPERSCRIPT TWO}",
    "direct_normal_irradiance": "W/m\N{SUPERSCRIPT TWO}",
    "diffuse_radiation": "W/m\N{SUPERSCRIPT TWO}",
    "precipitation": "mm",
}
MODELS = (
    "best_match",
    "ecmwf_ifs",
    "icon_seamless",
    "metno_seamless",
    "dmi_seamless",
    "knmi_seamless",
    "meteofrance_seamless",
    "ukmo_seamless",
    "gfs_seamless",
)
"""Models to offer in the settings. Open-Meteo has more; the API takes any of its names."""


class OpenMeteoPlugin(ForecastPlugin):
    name = "open_meteo"
    version = "0.1.0"
    root = "open_meteo"
    label = "Open-Meteo"

    def __init__(self, settings: OpenMeteo, *, url: str = URL, **kwargs: Any) -> None:
        super().__init__(settings.latitude, settings.longitude, **kwargs)
        self.settings = settings
        self._url = url

    def models(self) -> Knowledge[tuple[ForecastModel, ...]]:
        if self.settings.model == "best_match":
            return Knowledge(
                value=(
                    ForecastModel(
                        name="best_match: the most suitable models for the place, combined"
                    ),
                ),
                known="documented",
                basis=DOCS,
            )
        return Knowledge(
            value=(ForecastModel(name=self.settings.model),), known="user", basis="the settings"
        )

    def provider(self) -> Provider:
        conditions = [
            "the free API is for non-commercial use only, which includes personal home automation"
        ]
        license = "CC BY 4.0"
        if self.settings.model.startswith("ukmo"):
            license = "CC BY-SA 4.0 (data from UK Met Office models)"
            conditions.append(
                "data from UK Met Office models are shared further only under the same license"
            )
        return Provider(
            name="Open-Meteo",
            operator=Knowledge(value="Open-Meteo", known="documented", basis=DOCS),
            coverage=Knowledge(
                value=(
                    "the whole world; a regional model counts only where it covers the place,"
                    " and one that doesn't is left out without a word"
                ),
                known="documented",
                basis=DOCS,
            ),
            access=Access(
                key=Knowledge(value=False, known="documented", basis=DOCS),
                rate_limit=Knowledge(
                    value=(
                        "600 calls a minute, 5,000 an hour, 10,000 a day; more than 10"
                        " variables count as more than one call"
                    ),
                    known="documented",
                    basis=DOCS,
                ),
            ),
            terms=Terms(
                license=Knowledge(value=license, known="documented", basis=DOCS),
                attribution=Knowledge(
                    value="Weather data by Open-Meteo.com", known="documented", basis=DOCS
                ),
                conditions=Knowledge(value=tuple(conditions), known="documented", basis=DOCS),
                storing_allowed=Knowledge(
                    value=True, known="documented", basis=f"{DOCS}: {license}"
                ),
            ),
            verification=Knowledge(
                value=(
                    "no published scores; its archive of past forecasts lets anyone"
                    " compute their own"
                ),
                known="documented",
                basis=DOCS,
            ),
        )

    async def fetch(self, session: aiohttp.ClientSession) -> Fetched:
        params = {
            "latitude": str(self.latitude),
            "longitude": str(self.longitude),
            "hourly": ",".join((*INSTANT.values(), *PRECEDING_HOUR.values())),
            "wind_speed_unit": "ms",
            "timeformat": "unixtime",
            "forecast_days": str(DAYS),
        }
        if self.settings.model != "best_match":
            params["models"] = self.settings.model
        try:
            async with session.get(self._url, params=params) as answer:
                status, body = answer.status, await answer.read()
        except aiohttp.ClientError as e:
            raise SourceError(f"Open-Meteo can't be reached ({type(e).__name__})") from None
        if status == 400:
            raise Refused(f"Open-Meteo refused the request: {_reason(body)}")
        if status == 429:
            raise SourceError("Open-Meteo: too many requests")
        if status != 200:
            raise SourceError(f"Open-Meteo answered HTTP {status}: {_reason(body)}")
        try:
            document = json.loads(body)
        except ValueError:
            raise SourceError("Open-Meteo's answer isn't JSON") from None
        return Fetched(parse(document))


def _reason(body: bytes) -> str:
    try:
        reason = json.loads(body).get("reason")
    except (ValueError, AttributeError):
        return "no reason given"
    return str(reason) if reason else "no reason given"


def parse(document: dict[str, Any]) -> list[Interval]:
    try:
        hourly = document["hourly"]
        units = document.get("hourly_units", {})
        times = [datetime.fromtimestamp(t, UTC) for t in hourly["time"]]
    except (KeyError, TypeError, ValueError) as e:
        raise SourceError(f"Open-Meteo's answer can't be read ({type(e).__name__}: {e})") from None
    for name, expected in UNITS.items():
        if name in units and units[name] != expected:
            raise SourceError(f"Open-Meteo gives {name} in {units[name]!r}, not {expected!r}")
    out: list[Interval] = []
    for quantity, name in INSTANT.items():
        values = [_number(v) for v in hourly.get(name, ())]
        if len(values) == len(times) and any(v is not None for v in values):
            out += instants(quantity, times, values, None)
    hour = timedelta(hours=1)
    for quantity, name in PRECEDING_HOUR.items():
        values = [_number(v) for v in hourly.get(name, ())]
        if len(values) != len(times):
            continue
        for end, value in zip(times, values, strict=True):
            if value is not None:
                out.append(interval(quantity, end - hour, end, value, None))
    return out


def _number(raw: Any) -> float | None:
    return float(raw) if isinstance(raw, int | float) and not isinstance(raw, bool) else None


def create(context: PluginContext) -> OpenMeteoPlugin:
    """The entry point: a plugin instance from its settings."""
    return OpenMeteoPlugin(OpenMeteo.model_validate(dict(context.settings)))
