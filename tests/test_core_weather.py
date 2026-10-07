"""Weather in the core: the sun, derived values, the choice per quantity with fallbacks,
and forecasts kept at lead times and scored against the outdoor reference."""

import math
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from thermaestro.cap.model import Envelope, Interval, SeriesInfo
from thermaestro.core.sensors import dew_point, relative_humidity
from thermaestro.core.series import Key, Series
from thermaestro.core.sun import clear_sky, cos_zenith, sunlight
from thermaestro.core.values import Values
from thermaestro.core.weather import Span, Weather, next_hour, value_at
from thermaestro.store import Database, Location, WeatherChoice

T0 = datetime(2026, 1, 15, 10, tzinfo=UTC)
UNITS = {
    "temperature": "degC",
    "dew_point": "degC",
    "relative_humidity": "%",
    "cloud_cover": "%",
    "irradiance.global": "W/m2",
}


class Clock:
    def __init__(self, at: datetime) -> None:
        self.now = at.timestamp()

    def __call__(self) -> float:
        return self.now


def info(quantity: str, series: str | None = None) -> SeriesInfo:
    return SeriesInfo(
        id=series or quantity,
        kind="forecast",
        role="weather",
        unit=UNITS[quantity],
        resolution="PT1H",
        quantity=quantity,
    )


def hourly(series: str, quantity: str, start: datetime, hours: int, value: float) -> list[Interval]:
    return [
        Interval(
            series=series,
            start=start + timedelta(hours=n),
            end=start + timedelta(hours=n + 1),
            value=value,
            unit=UNITS[quantity],
            status="forecast",
        )
        for n in range(hours)
    ]


# --- the sun and humidity ------------------------------------------------------------------


def test_the_suns_height() -> None:
    noon = datetime(2026, 3, 20, 12, 7, tzinfo=UTC)  # the equinox, at the equator, 0° E
    assert cos_zenith(noon, 0.0, 0.0) > 0.999
    midsummer = datetime(2026, 6, 21, tzinfo=UTC)
    highest = max(
        math.degrees(math.asin(cos_zenith(midsummer + timedelta(minutes=m), 59.33, 18.07)))
        for m in range(0, 24 * 60, 2)
    )
    assert highest == pytest.approx(90 - 59.33 + 23.44, abs=0.3)  # Stockholm
    assert clear_sky(datetime(2026, 12, 21, 23, tzinfo=UTC), 59.33, 18.07) == 0.0
    assert clear_sky(noon, 0.0, 0.0) == pytest.approx(1098 * math.exp(-0.059), rel=0.01)


def test_sunlight_from_cloud_cover() -> None:
    start = datetime(2026, 6, 21, 10, tzinfo=UTC)
    clear = sunlight(start, start + timedelta(hours=1), 0, 59.33, 18.07)
    covered = sunlight(start, start + timedelta(hours=1), 100, 59.33, 18.07)
    assert 600 < clear < 900
    assert covered == pytest.approx(0.35 * clear)
    night = datetime(2026, 12, 21, 23, tzinfo=UTC)
    assert sunlight(night, night + timedelta(hours=1), 0, 59.33, 18.07) == 0.0


def test_humidity_and_dew_point_turn_into_each_other() -> None:
    for t, rh in ((-10.0, 90.0), (5.0, 76.5), (25.0, 40.0)):
        assert relative_humidity(t, dew_point(t, rh)) == pytest.approx(rh, abs=0.01)


def test_values_between_instants_and_in_periods() -> None:
    spans = [
        Span(T0, T0 + timedelta(hours=1), 10.0),
        Span(T0 + timedelta(hours=1), T0 + timedelta(hours=7), 16.0),
    ]
    half = T0 + timedelta(minutes=30)
    assert value_at(spans, half, instant=True) == 13.0
    assert value_at(spans, half, instant=False) == 10.0
    assert value_at(spans, T0 + timedelta(hours=8), instant=True) is None
    assert next_hour(T0) == T0
    assert next_hour(T0 + timedelta(minutes=1)) == T0 + timedelta(hours=1)


# --- providers, the choice and fallbacks -----------------------------------------------------


@pytest.fixture
async def weather(tmp_path: Path) -> AsyncIterator[tuple[Weather, Series, Values, Clock, Database]]:
    clock = Clock(T0)
    async with await Database.open(tmp_path / "t.db") as db:
        series = Series(db, clock)
        values = Values(db)
        await db.put(Location(latitude=59.33, longitude=18.07, timezone="Europe/Stockholm"))
        # MET Norway's way: temperature and humidity, cloud cover; no dew point, no sun.
        series.describe(
            "met", [info(q) for q in ("temperature", "relative_humidity", "cloud_cover")]
        )
        await series.put("met", hourly("temperature", "temperature", T0, 72, 2.0))
        await series.put("met", hourly("relative_humidity", "relative_humidity", T0, 72, 80.0))
        await series.put("met", hourly("cloud_cover", "cloud_cover", T0, 72, 50.0))
        # Open-Meteo's: everything, three days.
        series.describe("om", [info(q) for q in ("temperature", "dew_point", "irradiance.global")])
        await series.put("om", hourly("temperature", "temperature", T0, 72, 3.0))
        await series.put("om", hourly("dew_point", "dew_point", T0, 72, -1.0))
        await series.put("om", hourly("irradiance.global", "irradiance.global", T0, 72, 100.0))
        # Home Assistant's weather entity, as a fallback.
        entity = "weather.forecast_home"
        series.describe("ha", [info("temperature", f"{entity}/temperature")])
        await series.put("ha", hourly(f"{entity}/temperature", "temperature", T0, 24, 4.0))
        yield Weather(db, series, values, clock=clock), series, values, clock, db


async def test_the_providers_and_what_each_gives(
    weather: tuple[Weather, Series, Values, Clock, Database],
) -> None:
    w, *_ = weather
    sources = w.sources()
    assert sorted(sources) == ["ha:weather.forecast_home", "met", "om"]
    met = sources["met"]
    assert w.has(met, "temperature") == "offered"
    assert w.has(met, "dew_point") == "derived"
    assert w.has(met, "irradiance.global") == "derived"  # from cloud cover
    assert w.has(met, "wind_speed") is None
    assert w.has(sources["ha:weather.forecast_home"], "temperature") == "offered"


async def test_a_quantity_from_the_provider_chosen_for_it(
    weather: tuple[Weather, Series, Values, Clock, Database],
) -> None:
    w, _, _, _, db = weather
    await db.put(
        WeatherChoice(
            main="met",
            quantities={"irradiance.global": "om"},
            fallbacks=("ha:weather.forecast_home",),
        )
    )
    end = T0 + timedelta(days=2)
    temperature = await w.forecast("temperature", T0, end)
    assert (temperature.source, temperature.derived, temperature.fallback) == ("met", False, False)
    assert temperature.spans[0].value == 2.0
    dew = await w.forecast("dew_point", T0, end)
    assert (dew.source, dew.derived) == ("met", True)
    assert dew.spans[0].value == pytest.approx(dew_point(2.0, 80.0), abs=0.01)
    sun = await w.forecast("irradiance.global", T0, end)
    assert (sun.source, sun.derived, sun.spans[0].value) == ("om", False, 100.0)
    nothing = await w.forecast("wind_speed", T0, end)
    assert (nothing.source, nothing.spans) == (None, [])


async def test_a_fallback_stands_in_for_a_stale_forecast(
    weather: tuple[Weather, Series, Values, Clock, Database],
) -> None:
    w, _, _, clock, db = weather
    await db.put(WeatherChoice(main="om", fallbacks=("ha:weather.forecast_home",)))
    clock.now = (T0 + timedelta(hours=67)).timestamp()  # om reaches only 5 h ahead now
    later = datetime.fromtimestamp(clock.now, UTC)
    found = await w.forecast("temperature", later - timedelta(hours=30), later + timedelta(days=1))
    # The fallback's ends sooner still, so the chosen one is kept, marked stale.
    assert (found.source, found.stale) == ("om", True)
    clock.now = T0.timestamp()
    found = await w.forecast("temperature", T0, T0 + timedelta(days=1))
    assert (found.source, found.stale, found.fallback) == ("om", False, False)
    await db.put(WeatherChoice(main="nonexistent", fallbacks=("ha:weather.forecast_home",)))
    found = await w.forecast("temperature", T0, T0 + timedelta(days=1))
    assert (found.source, found.fallback) == ("ha:weather.forecast_home", True)


# --- scoring ----------------------------------------------------------------------------------


def measured(at: datetime, value: float) -> Envelope:
    return Envelope.model_validate(
        {
            "point": "outdoor/temperature",
            "value": value,
            "unit": "degC",
            "t_observed": at,
            "t_received": at,
            "quality": "good",
            "source": "measured",
        }
    )


async def test_forecasts_kept_and_scored_by_lead_time(
    weather: tuple[Weather, Series, Values, Clock, Database],
) -> None:
    w, _, values, clock, _ = weather
    clock.now = (T0 + timedelta(minutes=10)).timestamp()
    kept = await w.snapshot()
    # Temperature at 1, 6, 24 and 48 h from met and om, and the dew point from both (met's
    # derived); Home Assistant's forecast ends a day ahead, so only its 1 and 6 h.
    assert kept == 4 * 4 + 2
    # The outdoor sensor measured 1 °C lower than met's forecast, 2 lower than om's.
    for lead in (1, 6, 24, 48):
        valid = T0 + timedelta(hours=lead + 1)
        values.add("site", measured(valid - timedelta(minutes=5), 1.0))
    await values.flush()
    clock.now = (T0 + timedelta(hours=49, minutes=30)).timestamp()
    assert await w.observe() == 4
    scores = {(s.source, s.quantity, s.lead_h): s for s in await w.scores()}
    met = scores["met", "temperature", 24]
    assert (met.n, met.bias, met.mae) == (1, 1.0, 1.0)
    assert scores["om", "temperature", 48].bias == 2.0
    assert ("met", "dew_point", 1) not in scores  # no outdoor humidity, nothing to compare


async def test_old_forecasts_are_let_go(
    weather: tuple[Weather, Series, Values, Clock, Database],
) -> None:
    w, series, _, clock, _ = weather
    clock.now = (T0 + timedelta(days=20)).timestamp()
    await w.prune()
    assert await series.get(Key("met", "temperature"), 0, clock.now) == []
