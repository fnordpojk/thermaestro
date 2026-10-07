"""The weather plugins against stand-ins for MET Norway, SMHI and Open-Meteo."""

import asyncio
import contextlib
import itertools
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest
from weatherfake import FakeMet, FakeOpenMeteo, FakeSmhi, running

from thermaestro.cap import Link, pair, serve
from thermaestro.cap.conformance import run
from thermaestro.cap.messages import Health
from thermaestro.cap.model import Interval, Step
from thermaestro.core import discover
from thermaestro.forecast import ForecastPlugin, iso
from thermaestro.met_norway.plugin import MetNorwayPlugin
from thermaestro.open_meteo import archive
from thermaestro.open_meteo.plugin import OpenMeteoPlugin
from thermaestro.seriesplugin import Held
from thermaestro.smhi.plugin import SmhiPlugin
from thermaestro.store import OpenMeteo, WeatherPoint

OSLO = WeatherPoint(latitude=59.91391, longitude=10.75224)
STOCKHOLM = WeatherPoint(latitude=59.3293, longitude=18.0686)
BERLIN = OpenMeteo(latitude=52.52, longitude=13.405)
FAST: dict[str, Any] = {"min_wait_s": 0.05, "health_interval_s": 0.05, "jitter_s": 0}


def this_hour() -> datetime:
    return datetime.now(UTC).replace(minute=0, second=0, microsecond=0)


@contextlib.asynccontextmanager
async def connected(p: ForecastPlugin) -> AsyncIterator[tuple[Link, list[Health]]]:
    health: list[Health] = []
    core, plugin_side = pair()
    served = asyncio.create_task(serve(plugin_side, p))

    def heard(m: object) -> None:
        if isinstance(m, Health):
            health.append(m)

    try:
        async with Link(core, on_event=heard) as link:
            await link.hello(timeout=5)
            yield link, health
    finally:
        await plugin_side.close()
        served.cancel()
        await asyncio.gather(served, return_exceptions=True)


async def everything(link: Link, series: str) -> list[Interval]:
    now = datetime.now(UTC)
    data = await link.series_get(
        series, now - timedelta(days=1), now + timedelta(days=12), timeout=5
    )
    return list(data.intervals)


def test_durations_as_iso_writes_them() -> None:
    assert iso(timedelta(hours=1)) == "PT1H"
    assert iso(timedelta(minutes=15)) == "PT15M"
    assert iso(timedelta(hours=54)) == "P2DT6H"
    assert iso(timedelta(days=10)) == "P10D"
    assert iso(timedelta(days=1, minutes=30)) == "P1DT30M"


async def test_a_new_run_outranks_what_was_kept_before_a_restart() -> None:
    t = this_hour()

    def at(value: float) -> Interval:
        return Interval(
            series="temperature",
            start=t,
            end=t + timedelta(hours=1),
            value=value,
            unit="degC",
            status="forecast",
        )

    held = Held()
    assert held.keep([at(5.0)], revision=1000)
    assert held.all("temperature")[0].revision == 1000
    assert not held.keep([at(5.0)], revision=1060)  # unchanged: no new revision
    assert held.keep([at(6.0)], revision=900)  # changed: never lower than before
    assert held.all("temperature")[0].revision == 1001
    restarted = Held()
    restarted.keep([at(6.5)], revision=1120)
    assert restarted.all("temperature")[0].revision == 1120  # beats the 1001 kept


# --- MET Norway ------------------------------------------------------------------------------


async def test_met_norways_series_and_their_shape() -> None:
    start = this_hour()
    fake = FakeMet(start)
    async with running(fake) as url:
        p = MetNorwayPlugin(OSLO, url=url, **FAST)
        async with connected(p) as (link, _):
            described = await link.describe(timeout=5)
            infos = {s.id: s for s in described.series}
            temperature = await everything(link, "temperature")
            rain = await everything(link, "precipitation")
    assert set(infos) == {
        "temperature",
        "temperature.p10",
        "temperature.p90",
        "dew_point",
        "relative_humidity",
        "cloud_cover",
        "wind_speed",
        "wind_gust",
        "wind_direction",
        "pressure",
        "precipitation",
    }
    t = infos["temperature"]
    assert (t.kind, t.role, t.unit, t.quantity) == ("forecast", "weather", "degC", "temperature")
    assert t.percentiles == (10, 90)
    assert t.resolution == "PT1H"
    [hourly, six] = t.steps
    assert (hourly.step, six) == ("PT1H", Step(step="PT6H"))
    assert described.provider is not None
    assert described.provider.terms.attribution.value == "Data from MET Norway"
    first = temperature[0]
    assert (first.start, first.end, first.value) == (start, start + timedelta(hours=1), 5.0)
    assert first.status == "forecast"
    assert first.t_published == fake.updated
    assert first.revision == int(fake.updated.timestamp() // 60)
    assert [i.end - i.start for i in rain[:51]] == [timedelta(hours=1)] * 51
    assert rain[51].end - rain[51].start == timedelta(hours=6)
    assert all(a.end == b.start for a, b in itertools.pairwise(rain))
    asked = fake.requests[0]
    assert "github.com/fnordpojk/thermaestro" in asked.headers["User-Agent"]
    assert asked.headers["User-Agent"].startswith("Thermaestro-met_norway/")
    assert (asked.query["lat"], asked.query["lon"]) == ("59.914", "10.752")  # three decimals


async def test_met_norway_is_asked_again_only_when_its_answer_expires() -> None:
    fake = FakeMet(this_hour())
    fake.expires_s = 1
    async with running(fake) as url:
        p = MetNorwayPlugin(OSLO, url=url, **FAST)
        async with connected(p) as (link, _):
            await link.describe(timeout=5)
            async with link.series_subscribe("temperature") as subscription:
                first = await subscription.next(timeout=5)
                async with asyncio.timeout(10):
                    while len(fake.requests) < 2:
                        await asyncio.sleep(0.05)
                assert fake.requests[1].headers["If-Modified-Since"] == fake.last_modified
                fake.updated += timedelta(hours=1)
                fake.base = 6.0
                update = await subscription.next(timeout=10)
    assert first.intervals[0].value == 5.0
    assert update.intervals[0].value == 6.0
    assert update.intervals[0].revision == int(fake.updated.timestamp() // 60)


async def test_outside_the_nordic_area_met_norway_gives_less() -> None:
    fake = FakeMet(this_hour(), nordic=False)
    async with running(fake) as url:
        p = MetNorwayPlugin(OSLO, url=url, **FAST)
        async with connected(p) as (link, _):
            described = await link.describe(timeout=5)
    ids = {s.id for s in described.series}
    assert "temperature.p10" not in ids
    assert "wind_gust" not in ids
    assert next(s for s in described.series if s.id == "temperature").percentiles == ()


async def test_a_refusal_from_met_norway_stops_the_asking() -> None:
    fake = FakeMet(this_hour())
    fake.status = 403
    async with running(fake) as url:
        p = MetNorwayPlugin(OSLO, url=url, **FAST)
        async with connected(p) as (link, health):
            described = await link.describe(timeout=5)
            async with asyncio.timeout(5):
                while not health:
                    await asyncio.sleep(0.01)
            await asyncio.sleep(0.3)
    assert described.series == ()
    assert "refuses" in (health[-1].needs_user_action or "")
    assert len(fake.requests) == 1


# --- SMHI ---------------------------------------------------------------------------------


async def test_smhi_in_percent_and_by_its_periods() -> None:
    start = this_hour()
    fake = FakeSmhi(start)
    async with running(fake) as url:
        p = SmhiPlugin(STOCKHOLM, url=url, **FAST)
        async with connected(p) as (link, _):
            described = await link.describe(timeout=5)
            cloud = await everything(link, "cloud_cover")
            rain = await everything(link, "precipitation")
    ids = {s.id for s in described.series}
    assert "dew_point" not in ids  # SMHI gives none; the core derives it
    assert "irradiance.global" not in ids
    assert [c.value for c in cloud[:9]] == [n * 12.5 for n in range(9)]  # oktas
    assert (rain[0].start, rain[0].end) == (start - timedelta(hours=1), start)
    assert [r.end - r.start for r in rain[57:60]] == [timedelta(hours=h) for h in (1, 2, 3)]
    steps = next(s for s in described.series if s.id == "temperature").steps
    assert [s.step for s in steps] == ["PT1H", "PT2H", "PT3H", "PT6H", "PT12H"]
    assert described.provider is not None
    assert "oktas" in (described.provider.terms.attribution.value or "")


async def test_a_place_outside_smhis_grid() -> None:
    fake = FakeSmhi(this_hour())
    fake.inside = False
    async with running(fake) as url:
        p = SmhiPlugin(WeatherPoint(latitude=48.2082, longitude=16.3738), url=url, **FAST)
        async with connected(p) as (link, health):
            await link.describe(timeout=5)
            async with asyncio.timeout(5):
                while not health:
                    await asyncio.sleep(0.01)
    assert "outside SMHI's forecast area" in (health[-1].needs_user_action or "")


# --- Open-Meteo ---------------------------------------------------------------------------


async def test_open_meteos_hours_before_and_instants() -> None:
    start = this_hour()
    fake = FakeOpenMeteo(start)
    async with running(fake) as url:
        p = OpenMeteoPlugin(BERLIN, url=f"{url}/v1/forecast", **FAST)
        async with connected(p) as (link, _):
            described = await link.describe(timeout=5)
            sun = await everything(link, "irradiance.global")
            temperature = await everything(link, "temperature")
            rain = await everything(link, "precipitation")
    assert {s.id for s in described.series} >= {
        "irradiance.global",
        "irradiance.direct_normal",
        "irradiance.diffuse",
        "dew_point",
    }
    assert (sun[1].start, sun[1].end, sun[1].value) == (start, start + timedelta(hours=1), 10.0)
    assert (temperature[0].start, temperature[0].value) == (start, 11.9)
    assert len(rain) == 71  # the last hour has no value
    asked = fake.requests[0].query
    assert asked["wind_speed_unit"] == "ms"
    assert "models" not in asked
    assert described.provider is not None
    conditions = described.provider.terms.conditions.value or ()
    assert any("personal home automation" in c for c in conditions)


async def test_a_model_open_meteo_doesnt_know() -> None:
    fake = FakeOpenMeteo(this_hour())
    async with running(fake) as url:
        settings = OpenMeteo(latitude=52.52, longitude=13.405, model="nonsense")
        p = OpenMeteoPlugin(settings, url=f"{url}/v1/forecast", **FAST)
        async with connected(p) as (link, health):
            await link.describe(timeout=5)
            async with asyncio.timeout(5):
                while not health:
                    await asyncio.sleep(0.01)
    assert fake.requests[0].query["models"] == "nonsense"
    assert "Invalid value" in (health[-1].needs_user_action or "")


async def test_the_climate_from_open_meteos_archive() -> None:
    fake = FakeOpenMeteo(this_hour())
    async with running(fake) as url:
        normals = await archive.normals(52.52, 13.405, date(2026, 10, 7), url=f"{url}/v1/archive")
    asked = fake.requests[0].query
    assert (asked["start_date"], asked["end_date"]) == ("2016-01-01", "2025-12-31")
    assert normals.monthly_means == tuple(float(m) for m in range(1, 13))
    assert normals.monthly_spread == 11.0
    assert normals.annual_mean == pytest.approx(6.5, abs=0.1)
    assert normals.period == "2016\N{EN DASH}2025"


# --- all three ----------------------------------------------------------------------------


@pytest.mark.parametrize("which", ["met", "smhi", "open_meteo"])
async def test_they_conform(which: str) -> None:
    fakes: dict[str, Any] = {
        "met": (FakeMet, lambda u: MetNorwayPlugin(OSLO, url=u)),
        "smhi": (FakeSmhi, lambda u: SmhiPlugin(STOCKHOLM, url=u)),
        "open_meteo": (FakeOpenMeteo, lambda u: OpenMeteoPlugin(BERLIN, url=f"{u}/v1/forecast")),
    }
    fake_type, make = fakes[which]
    async with running(fake_type(this_hour())) as url:
        p = make(url)
        core, plugin_side = pair()
        served = asyncio.create_task(serve(plugin_side, p))
        try:
            assert list(await run(core, timeout_s=20, quiet_s=0.3)) == []
        finally:
            await plugin_side.close()
            served.cancel()
            await asyncio.gather(served, return_exceptions=True)


def test_they_are_installed() -> None:
    assert {"met_norway", "smhi", "open_meteo"} <= set(discover())
