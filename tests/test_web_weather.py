"""The weather page and API: providers, the register, the choice per quantity, the
forecast as used, and the climate."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from test_core_weather import hourly, info
from test_web import ADMIN_PASSWORD, FAST, KEY, ORIGIN, Clock, Site, csrf_of, form
from weatherfake import FakeOpenMeteo, running

from thermaestro.auth import Accounts, AddressLimiter, SetupCode
from thermaestro.cap.messages import Described
from thermaestro.core import AuditLog
from thermaestro.core.host import Instance, PluginHost, State
from thermaestro.core.series import Series
from thermaestro.core.values import Values
from thermaestro.core.weather import Weather
from thermaestro.met_norway.plugin import MetNorwayPlugin
from thermaestro.store import Database, Location, Plugin, SecretStore, WeatherPoint
from thermaestro.web import Services, create_app


@pytest.fixture
async def site(tmp_path: Path) -> AsyncIterator[Site]:
    clock = Clock()
    now = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
    async with await Database.open(tmp_path / "t.db") as db:
        audit = AuditLog(tmp_path / "audit")
        values = Values(db)
        secrets = SecretStore(tmp_path / "secrets.json")
        series = Series(db)
        host = PluginHost(
            db=db, secrets=secrets, values=values, audit=audit, factories={}, series=series
        )
        await db.put(Location(latitude=59.33, longitude=18.07, timezone="Europe/Stockholm"))
        place = {"latitude": 59.33, "longitude": 18.07}
        await db.put(Plugin(plugin="met_norway", settings=place), "met")
        await db.put(
            Plugin(plugin="open_meteo", settings={**place, "model": "icon_seamless"}), "om"
        )
        # What the two would have sent: MET Norway without dew point or sun, Open-Meteo
        # with both.
        host.instances["met"] = Instance(
            "met",
            Plugin(plugin="met_norway", settings=place),
            state=State.UP,
            described=Described(provider=MetNorwayPlugin(WeatherPoint(**place)).provider()),
        )
        series.describe(
            "met", [info(q) for q in ("temperature", "relative_humidity", "cloud_cover")]
        )
        await series.put("met", hourly("temperature", "temperature", now, 72, 2.0))
        await series.put("met", hourly("relative_humidity", "relative_humidity", now, 72, 80.0))
        await series.put("met", hourly("cloud_cover", "cloud_cover", now, 72, 50.0))
        series.describe("om", [info(q) for q in ("temperature", "irradiance.global")])
        await series.put("om", hourly("temperature", "temperature", now, 72, 3.0))
        await series.put("om", hourly("irradiance.global", "irradiance.global", now, 72, 100.0))
        services = Services(
            accounts=Accounts(
                db, audit, hasher=FAST, clock=clock, limiter=AddressLimiter(tries=1000)
            ),
            db=db,
            values=values,
            host=host,
            audit=audit,
            secrets=secrets,
            setup=SetupCode(tmp_path / "setup-code", clock),
            series=series,
            weather=Weather(db, series, values, host),
        )
        await services.load_zone()
        yield Site(create_app(services, KEY), services, clock, tmp_path)


@asynccontextmanager
async def logged_in(site: Site) -> AsyncIterator[httpx.AsyncClient]:
    await site.services.accounts.create_user("admin", ADMIN_PASSWORD, ["Administrators"], by="cli")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=site.app), base_url=ORIGIN, headers={"origin": ORIGIN}
    ) as client:
        page = await client.get("/login")
        await client.post(
            "/login", data={"csrf": csrf_of(page.text), "name": "admin", "password": ADMIN_PASSWORD}
        )
        yield client


async def call(client: httpx.AsyncClient, method: str, path: str, **kw: object) -> httpx.Response:
    """An API call from the browser's session: with the page's CSRF token."""
    token = csrf_of((await client.get("/weather")).text)
    return await client.request(method, path, headers={"x-csrf-token": token}, **kw)  # type: ignore[arg-type]


async def test_the_register_shows_what_each_provider_gives(site: Site) -> None:
    async with logged_in(site) as client:
        page = (await client.get("/setup/weather")).text
        register = (await client.get("/api/v1/weather")).json()
    assert "Data from MET Norway" in page
    assert "derived from Temperature, Humidity" in page  # MET Norway's dew point
    assert "sun gains can't be planned ahead" not in page  # sunlight is derived from clouds
    providers = {p["key"]: p for p in register["providers"]}
    assert sorted(providers) == ["met", "om"]
    met = {q["quantity"]: q for q in providers["met"]["quantities"]}
    assert (met["temperature"]["how"], met["temperature"]["freshness"]) == ("offered", "fresh")
    assert (met["dew_point"]["how"], met["dew_point"]["from"]) == (
        "derived",
        ["temperature", "relative_humidity"],
    )
    assert met["irradiance.global"]["how"] == "derived"
    assert met["wind_speed"]["how"] == "missing"
    assert providers["met"]["provider"]["terms"]["license"]["value"] == "CC BY 4.0 and NLOD 2.0"


async def test_choosing_a_provider_per_quantity(site: Site) -> None:
    async with logged_in(site) as client:
        await form(
            client,
            "/setup/weather",
            "/weather/choice",
            main="met",
            fallback="om",
            **{"q_irradiance.global": "om"},
        )
        choice = (await client.get("/api/v1/weather")).json()["choice"]
        assert choice == {
            "main": "met",
            "quantities": {"irradiance.global": "om"},
            "fallbacks": ["om"],
        }
        forecast = {f["quantity"]: f for f in (await client.get("/api/v1/weather/forecast")).json()}
        page = (await client.get("/weather")).text
        refused = await call(client, "PUT", "/api/v1/weather/choice", json={"main": "nowhere"})
    assert (forecast["temperature"]["source"], forecast["temperature"]["values"][0]["value"]) == (
        "met",
        2.0,
    )
    assert (forecast["dew_point"]["source"], forecast["dew_point"]["derived"]) == ("met", True)
    assert forecast["irradiance.global"]["values"][0]["value"] == 100.0
    assert forecast["wind_speed"]["source"] is None
    assert "* derived by Thermaestro" in page
    assert refused.status_code == 400
    assert "no weather provider 'nowhere'" in refused.json()["error"]


async def test_providers_follow_the_location(site: Site) -> None:
    async with logged_in(site) as client:
        await form(client, "/setup/weather", "/weather/sources", plugin="smhi")
        sources = (await client.get("/api/v1/weather/sources")).json()
        assert sources["smhi"]["settings"] == {"latitude": 59.33, "longitude": 18.07}
        await form(
            client,
            "/setup/house",
            "/settings/location",
            latitude="57.7",
            longitude="11.97",
            timezone="Europe/Stockholm",
        )
        sources = (await client.get("/api/v1/weather/sources")).json()
        assert {id: s["settings"]["latitude"] for id, s in sources.items()} == {
            "met": 57.7,
            "om": 57.7,
            "smhi": 57.7,
        }
        assert sources["om"]["settings"]["model"] == "icon_seamless"  # kept
        refused = await call(client, "PUT", "/api/v1/weather/sources/x", json={"plugin": "tibber"})
        assert refused.status_code == 400


async def test_a_removed_provider_leaves_the_choice(site: Site) -> None:
    async with logged_in(site) as client:
        await call(
            client,
            "PUT",
            "/api/v1/weather/choice",
            json={"main": "met", "quantities": {"irradiance.global": "om"}, "fallbacks": ["om"]},
        )
        await form(client, "/setup/weather", "/weather/sources/om/delete")
        choice = (await client.get("/api/v1/weather")).json()["choice"]
        sources = (await client.get("/api/v1/weather/sources")).json()
    assert choice == {"main": "met", "quantities": {}, "fallbacks": []}
    assert "om" not in sources


async def test_the_climate_entered_or_fetched(site: Site) -> None:
    async with logged_in(site) as client:
        await form(
            client, "/setup/house", "/weather/climate", annual_mean="7,4", monthly_spread="19.6"
        )
        climate = (await client.get("/api/v1/weather/climate")).json()
        assert (climate["annual_mean"], climate["monthly_spread"], climate["source"]) == (
            7.4,
            19.6,
            "user",
        )
        fake = FakeOpenMeteo(datetime.now(UTC))
        async with running(fake) as url:
            site.services.archive_url = f"{url}/v1/archive"
            fetched = (await call(client, "POST", "/api/v1/weather/climate/fetch")).json()
        page = (await client.get("/setup/house")).text
    assert fetched["source"] == "open_meteo"
    assert fetched["monthly_means"] == [float(m) for m in range(1, 13)]
    assert fetched["monthly_spread"] == 11.0
    assert "Open-Meteo's archive" in page
    asked = fake.requests[0].query
    assert (asked["latitude"], asked["longitude"]) == ("59.33", "18.07")


async def test_the_climate_needs_open_meteo_to_be_fetched(site: Site) -> None:
    async with logged_in(site) as client:
        await form(client, "/setup/weather", "/weather/sources/om/delete")
        refused = await call(client, "POST", "/api/v1/weather/climate/fetch")
    assert refused.status_code == 400
    assert "add Open-Meteo" in refused.json()["error"]
