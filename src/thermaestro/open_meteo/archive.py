"""The location's climate from Open-Meteo's archive (ERA5 reanalysis): the annual mean
temperature and the monthly means, over the last ten whole years.

Asked for once, when the user asks, as daily means: ten years of one variable, which
Open-Meteo counts as about 260 of its calls (one per two weeks), out of 10,000 a day.
"""

import json
from dataclasses import dataclass
from datetime import date

import aiohttp

from ..seriesplugin import SourceError, user_agent

URL = "https://archive-api.open-meteo.com/v1/archive"
YEARS = 10


@dataclass(frozen=True)
class Normals:
    annual_mean: float
    monthly_means: tuple[float, ...]
    """January to December."""
    period: str

    @property
    def monthly_spread(self) -> float:
        return round(max(self.monthly_means) - min(self.monthly_means), 1)


def years(today: date) -> tuple[int, int]:
    """The last ten whole years."""
    return today.year - YEARS, today.year - 1


async def normals(latitude: float, longitude: float, today: date, *, url: str = URL) -> Normals:
    first, last = years(today)
    params = {
        "latitude": str(round(latitude, 3)),
        "longitude": str(round(longitude, 3)),
        "start_date": f"{first}-01-01",
        "end_date": f"{last}-12-31",
        "daily": "temperature_2m_mean",
        "timezone": "auto",
    }
    headers = {"User-Agent": user_agent("open_meteo", "0.1.0")}
    timeout = aiohttp.ClientTimeout(total=120)
    try:
        async with (
            aiohttp.ClientSession(headers=headers, timeout=timeout) as session,
            session.get(url, params=params) as answer,
        ):
            status, body = answer.status, await answer.read()
    except (aiohttp.ClientError, TimeoutError) as e:
        raise SourceError(f"Open-Meteo's archive can't be reached ({type(e).__name__})") from None
    if status != 200:
        raise SourceError(f"Open-Meteo's archive answered HTTP {status}")
    return parse(body, f"{first}\N{EN DASH}{last}")


def parse(body: bytes, period: str) -> Normals:
    try:
        daily = json.loads(body)["daily"]
        days = [date.fromisoformat(d) for d in daily["time"]]
        values = daily["temperature_2m_mean"]
    except (ValueError, KeyError, TypeError) as e:
        raise SourceError(f"Open-Meteo's archive answer can't be read ({e})") from None
    by_month: dict[int, list[float]] = {m: [] for m in range(1, 13)}
    every: list[float] = []
    for day, value in zip(days, values, strict=False):
        if isinstance(value, int | float):
            by_month[day.month].append(float(value))
            every.append(float(value))
    if any(not v for v in by_month.values()):
        raise SourceError("Open-Meteo's archive has no temperatures for some months")
    monthly = tuple(round(sum(v) / len(v), 1) for v in by_month.values())
    return Normals(round(sum(every) / len(every), 1), monthly, period)
