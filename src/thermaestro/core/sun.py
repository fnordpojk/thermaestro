"""The sun's height, and sunlight estimated from cloud cover where a provider gives none.

- **Position:** NOAA's general solar position equations (the NOAA Global Monitoring
  Laboratory's "General Solar Position Calculations"), good to a fraction of a degree:
  enough for an estimate whose error is the cloud cover's.
- **Clear sky:** Haurwitz's model, which needs only the sun's height:
  1098 · cos z · exp(-0.059 / cos z) W/m².
- **Clouds:** the clear-sky value scaled linearly with cloud cover, to 35 % of it under a
  full cover (Larson et al. 2016, as pvlib implements it).
"""

import math
from datetime import UTC, datetime, timedelta

CLOUDED = 0.35
"""What a full cloud cover leaves of the clear-sky sunlight."""
SAMPLE = timedelta(minutes=5)


def cos_zenith(t: datetime, latitude: float, longitude: float) -> float:
    """The cosine of the sun's angle from straight up; below 0 at night."""
    u = t.astimezone(UTC)
    day = u.timetuple().tm_yday
    hours = u.hour + u.minute / 60 + u.second / 3600
    g = 2 * math.pi / 365 * (day - 1 + (hours - 12) / 24)
    equation_of_time = 229.18 * (
        0.000075
        + 0.001868 * math.cos(g)
        - 0.032077 * math.sin(g)
        - 0.014615 * math.cos(2 * g)
        - 0.040849 * math.sin(2 * g)
    )
    declination = (
        0.006918
        - 0.399912 * math.cos(g)
        + 0.070257 * math.sin(g)
        - 0.006758 * math.cos(2 * g)
        + 0.000907 * math.sin(2 * g)
        - 0.002697 * math.cos(3 * g)
        + 0.00148 * math.sin(3 * g)
    )
    solar_minutes = hours * 60 + equation_of_time + 4 * longitude
    hour_angle = math.radians(solar_minutes / 4 - 180)
    phi = math.radians(latitude)
    return math.sin(phi) * math.sin(declination) + math.cos(phi) * math.cos(declination) * math.cos(
        hour_angle
    )


def clear_sky(t: datetime, latitude: float, longitude: float) -> float:
    """Global horizontal sunlight under a clear sky, W/m²."""
    c = cos_zenith(t, latitude, longitude)
    return 1098.0 * c * math.exp(-0.059 / c) if c > 0 else 0.0


def sunlight(
    start: datetime, end: datetime, cloud_cover: float, latitude: float, longitude: float
) -> float:
    """The mean global horizontal sunlight over a period, W/m², from its cloud cover (%)."""
    samples = []
    t = start + SAMPLE / 2
    while t < end:
        samples.append(clear_sky(t, latitude, longitude))
        t += SAMPLE
    clear = sum(samples) / len(samples) if samples else 0.0
    cover = min(max(cloud_cover, 0.0), 100.0) / 100
    return clear * (CLOUDED + (1 - CLOUDED) * (1 - cover))
