"""The Home Assistant plugin against a stand-in for HA's WebSocket API."""

import asyncio
import contextlib
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from hafake import TOKEN, FakeHomeAssistant, running

from thermaestro.cap import Link, pair, serve
from thermaestro.cap.conformance import run
from thermaestro.cap.messages import Health
from thermaestro.core import discover
from thermaestro.homeassistant.plugin import HomeAssistantPlugin, list_entities
from thermaestro.store import HomeAssistant, SecretStore

ENTITIES = (
    "sensor.bedroom_temperature",
    "sensor.bedroom_humidity",
    "sensor.porch_temperature_f",
    "sensor.radiator_pi_heating_demand",
    "binary_sensor.kitchen_window",
    "climate.living_room",
    "sensor.gone",
)
OWN = "ha/x.homeassistant."


def stocked() -> FakeHomeAssistant:
    fake = FakeHomeAssistant()
    fake.set(
        "sensor.bedroom_temperature",
        "20.5",
        device_class="temperature",
        unit_of_measurement="°C",
        friendly_name="Bedroom temperature",
    )
    fake.set(
        "sensor.bedroom_humidity",
        "41",
        device_class="humidity",
        unit_of_measurement="%",
        friendly_name="Bedroom humidity",
    )
    fake.set(
        "sensor.porch_temperature_f",
        "41.0",
        device_class="temperature",
        unit_of_measurement="°F",
        friendly_name="Porch",
    )
    fake.set(
        "sensor.radiator_pi_heating_demand",
        "37",
        unit_of_measurement="%",
        friendly_name="Radiator demand",
    )
    fake.set(
        "binary_sensor.kitchen_window", "off", device_class="window", friendly_name="Kitchen window"
    )
    fake.set(
        "climate.living_room",
        "heat",
        current_temperature=21.0,
        temperature=21.5,
        hvac_action="heating",
        pi_heating_demand=55,
        friendly_name="Living room thermostat",
    )
    fake.set("sensor.gone", "unavailable", device_class="temperature", unit_of_measurement="°C")
    fake.set("sensor.uptime", "12345", friendly_name="Uptime")  # nothing worth reading
    fake.set("light.hall", "on")
    fake.entity_areas = {"sensor.bedroom_temperature": "bed", "climate.living_room": "living"}
    return fake


async def secrets(tmp_path: Path, token: str = TOKEN) -> SecretStore:
    store = SecretStore(tmp_path / "secrets.json")
    await store.set("ha.token", token)
    return store


@contextlib.asynccontextmanager
async def plugin(
    url: str, store: SecretStore, entities: tuple[str, ...] = ENTITIES
) -> AsyncIterator[tuple[HomeAssistantPlugin, Link]]:
    p = HomeAssistantPlugin(
        HomeAssistant(url=url, token="ha.token", entities=entities),
        secrets=store,
        health_interval_s=0.2,
    )
    core, plugin_side = pair()
    served = asyncio.create_task(serve(plugin_side, p))
    link = Link(core)
    link.start()
    try:
        await link.hello(timeout=5)
        yield p, link
    finally:
        await link.close()
        await plugin_side.close()
        served.cancel()
        await asyncio.gather(served, return_exceptions=True)


def value(p: HomeAssistantPlugin, path: str) -> tuple[object, str | None, str, str | None]:
    e = p.envelope(path)
    return e.value, e.unit, e.quality, e.why


async def test_values_in_stored_units(tmp_path: Path) -> None:
    fake = stocked()
    async with running(fake) as url, plugin(url, await secrets(tmp_path)) as (p, link):
        described = await link.describe(timeout=5)
        paths = {pt.path for pt in described.points}
        assert f"{OWN}sensor.bedroom_temperature" in paths
        assert {"ha/climate.living_room/temperature", "ha/climate.living_room/heat_demand"} <= paths
        assert "ha/climate.living_room/window.open" not in paths  # it doesn't report windows
        labels = {pt.path: pt.label for pt in described.points}
        assert labels[f"{OWN}sensor.bedroom_temperature"] == "Bedroom temperature"
        assert {n.path: n.kind for n in described.nodes}["ha/climate.living_room"] == "room"
        assert value(p, f"{OWN}sensor.bedroom_temperature") == (20.5, "degC", "good", None)
        assert value(p, f"{OWN}sensor.porch_temperature_f") == (5.0, "degC", "good", None)
        assert value(p, f"{OWN}sensor.radiator_pi_heating_demand")[:3] == (37.0, "%", "good")
        assert value(p, f"{OWN}binary_sensor.kitchen_window")[:3] == (False, None, "good")
        assert value(p, "ha/climate.living_room/temperature")[:3] == (21.0, "degC", "good")
        assert value(p, "ha/climate.living_room/setpoint")[:3] == (21.5, "degC", "good")
        assert value(p, "ha/climate.living_room/zone.open")[:3] == (True, None, "good")
        assert value(p, "ha/climate.living_room/heat_demand")[:3] == (55.0, "%", "good")
        gone = value(p, f"{OWN}sensor.gone")
        assert gone == (None, None, "unknown", "Home Assistant: unavailable")
        assert fake.subscribed == [list(ENTITIES)]  # only what was chosen


async def test_changes_arrive(tmp_path: Path) -> None:
    fake = stocked()
    temperature, heating = f"{OWN}sensor.bedroom_temperature", "ha/climate.living_room/zone.open"
    async with running(fake) as url, plugin(url, await secrets(tmp_path)) as (_, link):
        await link.describe(timeout=5)
        async with link.subscribe([temperature, heating]) as sub:
            first = await sub.next(timeout=5)
            assert {e.point: e.value for e in first.values}[temperature] == 20.5
            await fake.change("sensor.bedroom_temperature", "21.25")
            await fake.change("climate.living_room", hvac_action="idle")
            seen: dict[str, object] = {}
            async with asyncio.timeout(5):
                while seen.get(temperature) != 21.25 or seen.get(heating) is not False:
                    update = await sub.next(timeout=5)
                    seen.update({e.point: e.value for e in update.values})


async def test_a_refused_token_asks_for_a_new_one(tmp_path: Path) -> None:
    fake = stocked()
    events: list[object] = []

    async def send(message: object) -> None:
        events.append(message)

    async with running(fake) as url:
        p = HomeAssistantPlugin(
            HomeAssistant(url=url, token="ha.token", entities=ENTITIES),
            secrets=await secrets(tmp_path, "wrong"),
        )
        with pytest.raises(Exception, match="Invalid access token"):
            await p.events(send)
    health = [e for e in events if isinstance(e, Health)]
    assert health[-1].needs_user_action is not None
    assert "new long-lived token" in health[-1].needs_user_action


async def test_nothing_chosen_reads_nothing(tmp_path: Path) -> None:
    fake = stocked()
    async with running(fake) as url, plugin(url, await secrets(tmp_path), entities=()) as (_, link):
        described = await link.describe(timeout=5)
        assert described.points == ()
        assert fake.subscribed == []


async def test_entities_to_choose_from() -> None:
    fake = stocked()
    # Thermaestro's own, from discovery: never offered back as a sensor.
    fake.set(
        "sensor.thermaestro_living_room_temperature",
        "21.0",
        device_class="temperature",
        unit_of_measurement="°C",
    )
    fake.registered["sensor.thermaestro_living_room_temperature"] = {
        "platform": "mqtt",
        "unique_id": "thermaestro_abcd1234_site_room_living_temperature_123abc",
    }
    async with running(fake) as url:
        found = await list_entities(url, TOKEN)
    by_id = {e.entity_id: e for e in found}
    assert "sensor.uptime" not in by_id
    assert "sensor.thermaestro_living_room_temperature" not in by_id
    assert "light.hall" not in by_id
    assert by_id["sensor.bedroom_temperature"].quantity == "temperature"
    assert by_id["sensor.bedroom_temperature"].area == "Bedroom"
    assert by_id["sensor.bedroom_temperature"].points == (
        (f"{OWN}sensor.bedroom_temperature", "temperature"),
    )
    assert by_id["binary_sensor.kitchen_window"].quantity == "window.open"
    assert by_id["sensor.radiator_pi_heating_demand"].quantity == "heat_demand"
    climate = by_id["climate.living_room"]
    assert climate.area == "Living room"
    assert ("ha/climate.living_room/heat_demand", "heat_demand") in climate.points
    assert found[0].area == "Bedroom"  # by area, then name


async def test_weather_entities_are_listed_as_providers() -> None:
    fake = stocked()
    with_weather(fake, datetime.now(UTC))
    async with running(fake) as url:
        found = {e.entity_id: e for e in await list_entities(url, TOKEN)}
    assert found[WEATHER].domain == "weather"
    assert found[WEATHER].points == ()  # not a sensor


WEATHER = "weather.forecast_home"


def with_weather(fake: FakeHomeAssistant, start: datetime) -> None:
    """A weather entity in American units, with an hourly forecast."""
    fake.set(
        WEATHER,
        "cloudy",
        temperature=41.0,
        temperature_unit="°F",
        pressure_unit="inHg",
        wind_speed_unit="mph",
        precipitation_unit="in",
        friendly_name="Forecast Home",
    )
    fake.forecasts[WEATHER] = [
        {
            "datetime": (start + timedelta(hours=n)).isoformat(),
            "condition": "cloudy",
            "temperature": 41.0 + n,
            "dew_point": 32.0,
            "humidity": 80,
            "cloud_coverage": 75,
            "pressure": 29.92,
            "wind_speed": 22.37,
            "wind_bearing": 270.0,
            "precipitation": 0.1,
        }
        for n in range(24)
    ]


async def test_a_weather_entitys_forecast_as_series(tmp_path: Path) -> None:
    fake = stocked()
    start = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
    with_weather(fake, start)
    entities = (*ENTITIES, WEATHER)
    async with running(fake) as url, plugin(url, await secrets(tmp_path), entities) as (_, link):
        described = await link.describe(timeout=5)
        ids = {s.id for s in described.series}
        later = start + timedelta(days=2)
        got = {
            q: (await link.series_get(f"{WEATHER}/{q}", start, later, timeout=5)).intervals[0]
            for q in ("temperature", "pressure", "wind_speed", "precipitation", "wind_direction")
        }
        async with link.series_subscribe(f"{WEATHER}/temperature") as subscription:
            await subscription.next(timeout=5)
            changed = [dict(e, temperature=50.0) for e in fake.forecasts[WEATHER]]
            await fake.new_forecast(WEATHER, changed)
            update = await subscription.next(timeout=5)
    assert {f"{WEATHER}/temperature", f"{WEATHER}/dew_point", f"{WEATHER}/cloud_cover"} <= ids
    assert f"{WEATHER}/irradiance.global" not in ids  # HA's weather has no sunlight
    assert not any("weather." in p.path for p in described.points)
    info = next(s for s in described.series if s.id == f"{WEATHER}/temperature")
    assert (info.kind, info.quantity, info.unit, info.resolution) == (
        "forecast",
        "temperature",
        "degC",
        "PT1H",
    )
    assert described.provider is not None
    assert described.provider.name == "Home Assistant"
    assert (got["temperature"].start, got["temperature"].value) == (start, 5.0)
    assert got["pressure"].value == pytest.approx(1013.2, abs=0.1)
    assert got["wind_speed"].value == pytest.approx(10.0, abs=0.01)
    assert got["precipitation"].value == pytest.approx(2.54)
    assert got["wind_direction"].value == 270.0
    assert update.intervals[0].value == 10.0  # 50 °F


async def test_a_weather_entity_without_an_hourly_forecast(tmp_path: Path) -> None:
    fake = stocked()
    fake.set("weather.daily_only", "sunny", temperature_unit="°C")
    entities = (*ENTITIES, "weather.daily_only")
    async with running(fake) as url, plugin(url, await secrets(tmp_path), entities) as (_, link):
        described = await link.describe(timeout=5)
    assert described.series == ()
    assert described.provider is None


async def test_it_conforms(tmp_path: Path) -> None:
    fake = stocked()
    with_weather(fake, datetime.now(UTC).replace(minute=0, second=0, microsecond=0))
    async with running(fake) as url:
        p = HomeAssistantPlugin(
            HomeAssistant(url=url, token="ha.token", entities=(*ENTITIES, WEATHER)),
            secrets=await secrets(tmp_path),
        )
        core, plugin_side = pair()
        served = asyncio.create_task(serve(plugin_side, p))
        try:
            assert list(await run(core, timeout_s=20, quiet_s=0.3)) == []
        finally:
            await plugin_side.close()
            served.cancel()
            await asyncio.gather(served, return_exceptions=True)


def test_the_entry_point_is_registered() -> None:
    assert "homeassistant" in discover()
