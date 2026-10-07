"""Stand-ins for the three weather APIs: MET Norway's Locationforecast, SMHI's snow1g and
Open-Meteo's forecast and archive. Each answers in its API's format (recorded from real
answers at city centers, 2026-10-07) with invented values, and remembers what it was
asked."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime, timedelta
from email.utils import format_datetime
from typing import Any

from aiohttp import web
from aiohttp.test_utils import TestServer

HOUR = timedelta(hours=1)


def hours_from(start: datetime, steps: list[tuple[int, int]]) -> list[datetime]:
    """Times from `start`: `steps` is (step in hours, how many), in order."""
    out, t = [], start
    for step, n in steps:
        for _ in range(n):
            out.append(t)
            t += timedelta(hours=step)
    return out


class FakeMet:
    def __init__(self, start: datetime, *, nordic: bool = True) -> None:
        self.start = start
        self.nordic = nordic
        self.updated = start - timedelta(minutes=30)
        self.expires_s = 1800.0
        self.status = 200
        self.requests: list[web.Request] = []
        self.base = 5.0

    @property
    def last_modified(self) -> str:
        return format_datetime(self.updated, usegmt=True)

    def document(self) -> dict[str, Any]:
        times = hours_from(self.start, [(1, 51), (6, 31)])
        series = []
        for n, t in enumerate(times):
            details: dict[str, float] = {
                "air_pressure_at_sea_level": 1014.4,
                "air_temperature": round(self.base + 0.1 * n, 1),
                "cloud_area_fraction": float(n % 9 * 12),
                "dew_point_temperature": round(self.base - 4 + 0.1 * n, 1),
                "relative_humidity": 76.5,
                "wind_from_direction": 92.0,
                "wind_speed": 0.7,
            }
            if self.nordic:
                details |= {
                    "air_temperature_percentile_10": round(self.base - 1 + 0.1 * n, 1),
                    "air_temperature_percentile_90": round(self.base + 1 + 0.1 * n, 1),
                    "wind_speed_of_gust": 1.4,
                }
            data: dict[str, Any] = {"instant": {"details": details}}
            if n < 51:
                data["next_1_hours"] = {
                    "summary": {"symbol_code": "cloudy"},
                    "details": {"precipitation_amount": 0.1, "precipitation_amount_max": 0.3},
                }
            if n < len(times) - 1:
                data["next_6_hours"] = {
                    "summary": {"symbol_code": "cloudy"},
                    "details": {"air_temperature_max": 7.9, "precipitation_amount": 0.6},
                }
            series.append({"time": t.strftime("%Y-%m-%dT%H:%M:%SZ"), "data": data})
        return {
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [10.752, 59.914, 5]},
            "properties": {
                "meta": {
                    "updated_at": self.updated.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "units": {
                        "air_pressure_at_sea_level": "hPa",
                        "air_temperature": "celsius",
                        "cloud_area_fraction": "%",
                        "dew_point_temperature": "celsius",
                        "precipitation_amount": "mm",
                        "relative_humidity": "%",
                        "wind_from_direction": "degrees",
                        "wind_speed": "m/s",
                    },
                },
                "timeseries": series,
            },
        }

    async def handler(self, request: web.Request) -> web.Response:
        self.requests.append(request)
        expires = datetime.now(UTC) + timedelta(seconds=self.expires_s)
        headers = {
            "Expires": format_datetime(expires, usegmt=True),
            "Last-Modified": self.last_modified,
        }
        if self.status != 200:
            return web.Response(status=self.status, text="refused")
        if request.headers.get("If-Modified-Since") == self.last_modified:
            return web.Response(status=304, headers=headers)
        return web.json_response(self.document(), headers=headers)


class FakeSmhi:
    def __init__(self, start: datetime) -> None:
        self.start = start
        self.created = start - timedelta(minutes=15)
        self.requests: list[web.Request] = []
        self.inside = True

    def document(self) -> dict[str, Any]:
        times = hours_from(self.start, [(1, 57), (2, 1), (3, 1), (6, 13), (12, 9)])
        series = []
        for n, t in enumerate(times):
            before = times[n - 1] if n else t - HOUR
            series.append(
                {
                    "time": t.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "intervalParametersStartTime": before.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "data": {
                        "air_temperature": round(8.5 + 0.1 * n, 1),
                        "wind_from_direction": 262,
                        "wind_speed": 3.4,
                        "wind_speed_of_gust": 6.2,
                        "relative_humidity": 79,
                        "air_pressure_at_mean_sea_level": 1012.1,
                        "visibility_in_air": 18.5,
                        "cloud_area_fraction": n % 9,
                        "low_type_cloud_area_fraction": 0,
                        "precipitation_amount_mean": 0.2 if n % 2 else 0.0,
                        "precipitation_frozen_part": -9,
                        "symbol_code": 1,
                    },
                }
            )
        return {
            "createdTime": self.created.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "referenceTime": self.created.strftime("%Y-%m-%dT%H:00:00Z"),
            "geometry": {"type": "Point", "coordinates": [18.077207, 59.33036]},
            "timeSeries": series,
        }

    async def handler(self, request: web.Request) -> web.Response:
        self.requests.append(request)
        if not self.inside:
            return web.Response(status=404, text="Requested point is out of bounds")
        expires = datetime.now(UTC) + HOUR
        return web.json_response(
            self.document(),
            headers={
                "Expires": format_datetime(expires, usegmt=True),
                "Cache-Control": "max-age=3600,public",
            },
        )


class FakeOpenMeteo:
    def __init__(self, start: datetime) -> None:
        self.start = start
        self.requests: list[web.Request] = []

    def document(self) -> dict[str, Any]:
        times = [int((self.start + n * HOUR).timestamp()) for n in range(72)]
        return {
            "latitude": 52.512604,
            "longitude": 13.419517,
            "generationtime_ms": 0.4,
            "utc_offset_seconds": 0,
            "timezone": "GMT",
            "timezone_abbreviation": "GMT",
            "elevation": 37.0,
            "hourly_units": {
                "time": "unixtime",
                "temperature_2m": "\N{DEGREE SIGN}C",
                "dew_point_2m": "\N{DEGREE SIGN}C",
                "relative_humidity_2m": "%",
                "cloud_cover": "%",
                "shortwave_radiation": "W/m\N{SUPERSCRIPT TWO}",
                "direct_normal_irradiance": "W/m\N{SUPERSCRIPT TWO}",
                "diffuse_radiation": "W/m\N{SUPERSCRIPT TWO}",
                "wind_speed_10m": "m/s",
                "wind_gusts_10m": "m/s",
                "wind_direction_10m": "\N{DEGREE SIGN}",
                "precipitation": "mm",
                "pressure_msl": "hPa",
            },
            "hourly": {
                "time": times,
                "temperature_2m": [round(11.9 + 0.1 * n, 1) for n in range(72)],
                "dew_point_2m": [round(9.5 + 0.1 * n, 1) for n in range(72)],
                "relative_humidity_2m": [85] * 72,
                "cloud_cover": [50] * 72,
                "shortwave_radiation": [float(n % 24 * 10) for n in range(72)],
                "direct_normal_irradiance": [0.0] * 72,
                "diffuse_radiation": [float(n % 24 * 5) for n in range(72)],
                "wind_speed_10m": [0.86] * 72,
                "wind_gusts_10m": [3.1] * 72,
                "wind_direction_10m": [126] * 72,
                "precipitation": [0.0] * 71 + [None],
                "pressure_msl": [1017.7] * 72,
            },
        }

    async def handler(self, request: web.Request) -> web.Response:
        self.requests.append(request)
        if request.query.get("models", "best_match") not in ("best_match", "icon_seamless"):
            return web.json_response(
                {
                    "reason": "Invalid value: Cannot initialize MultiDomains from invalid String"
                    f" value {request.query['models']}",
                    "error": True,
                },
                status=400,
            )
        return web.json_response(self.document())

    async def archive(self, request: web.Request) -> web.Response:
        self.requests.append(request)
        first = date.fromisoformat(request.query["start_date"])
        last = date.fromisoformat(request.query["end_date"])
        days, values = [], []
        d = first
        while d <= last:
            days.append(d.isoformat())
            values.append(float(d.month))  # a month's mean is its number
            d += timedelta(days=1)
        return web.json_response(
            {
                "latitude": 52.54833,
                "longitude": 13.407822,
                "timezone": "Europe/Berlin",
                "daily_units": {"time": "iso8601", "temperature_2m_mean": "\N{DEGREE SIGN}C"},
                "daily": {"time": days, "temperature_2m_mean": values},
            }
        )


@asynccontextmanager
async def running(fake: FakeMet | FakeSmhi | FakeOpenMeteo) -> AsyncIterator[str]:
    """Serve the fake; yields its URL."""
    app = web.Application()
    if isinstance(fake, FakeOpenMeteo):
        app.router.add_get("/v1/archive", fake.archive)  # before the catch-all
    app.router.add_get("/{tail:.*}", fake.handler)
    server = TestServer(app, host="127.0.0.1")
    await server.start_server()
    try:
        yield f"http://127.0.0.1:{server.port}"
    finally:
        await server.close()
