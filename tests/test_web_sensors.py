"""Sensors, rooms, the outdoor references, names and Home Assistant in the web UI and API."""

import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest
from capfake import FakePump
from hafake import TOKEN, FakeHomeAssistant, running
from test_web import ADMIN_PASSWORD, FAST, KEY, ORIGIN, Clock, Site, csrf_of, form

from thermaestro.auth import Accounts, AddressLimiter, SetupCode
from thermaestro.core import AuditLog
from thermaestro.core.host import Instance, PluginHost, State
from thermaestro.core.sensors import SITE, SensorHub
from thermaestro.core.values import Key, Values
from thermaestro.store import Database, Names, Plugin, Room, SecretStore, Sensor
from thermaestro.web import Services, create_app


@pytest.fixture
async def site(tmp_path: Path) -> AsyncIterator[Site]:
    clock = Clock()
    async with await Database.open(tmp_path / "t.db") as db:
        audit = AuditLog(tmp_path / "audit")
        values = Values(db)
        secrets = SecretStore(tmp_path / "secrets.json")
        accounts = Accounts(db, audit, hasher=FAST, clock=clock, limiter=AddressLimiter(tries=1000))
        host = PluginHost(db=db, secrets=secrets, values=values, audit=audit, factories={})
        hub = SensorHub(db, values)
        await hub.load()
        services = Services(
            accounts=accounts,
            db=db,
            values=values,
            host=host,
            audit=audit,
            secrets=secrets,
            setup=SetupCode(tmp_path / "setup-code", clock),
            sensors=hub,
        )
        pump = FakePump()
        described = pump.describe(1)
        host.instances["pump"] = Instance(
            "pump", Plugin(plugin="fake"), state=State.UP, described=described
        )
        for point in described.points:
            values.add("pump", pump.envelope(point.path))
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


async def test_rooms_and_their_sensors(site: Site) -> None:
    async with logged_in(site) as client:
        rooms = (await client.get("/rooms")).text
        assert '<option value="pump:hp1/cs1">Climate system 1</option>' in rooms
        made = await form(
            client,
            "/rooms",
            "/rooms",
            name="Living room",
            climate_system="pump:hp1/cs1",
            own_device="simple_thermostat",
        )
        assert made.status_code == 303
        added = await form(
            client,
            "/sensors",
            "/sensors",
            name="Sofa",
            topic="zigbee2mqtt/sofa",
            json_key="temperature",
            quantity="temperature",
            placement="room",
            room="living-room",
        )
        assert added.status_code == 303
        hub = site.services.sensors
        assert hub is not None
        assert hub.topics() == ["zigbee2mqtt/sofa"]
        hub.receive_mqtt("zigbee2mqtt/sofa", b'{"temperature": 21.5}')
        status = (await client.get("/status/values")).text
        assert "<h2>Living room</h2>" in status
        assert "21.5 \N{DEGREE SIGN}C" in status
        rooms = (await client.get("/rooms")).text
        assert "Sofa" in rooms
        assert "turn the thermostat up" in rooms  # the advice for a simple thermostat
        site_api = (await client.get("/api/v1/site")).json()
        assert site_api["rooms"][0]["points"][0]["value"] == 21.5
        # Removing the room keeps the sensor, without a room.
        await form(client, "/rooms", "/rooms/living-room/delete")
        sensor = await site.services.db.get(Sensor, "sofa")
        assert sensor is not None
        assert sensor.room is None


async def test_bad_sensors_are_refused(site: Site) -> None:
    async with logged_in(site) as client:
        token = csrf_of((await client.get("/sensors")).text)
        refused = await client.post(
            "/api/v1/sensors",
            json={"name": "x", "source": "mqtt", "topic": "t", "room": "nowhere"},
            headers={"x-csrf-token": token},
        )
        assert refused.status_code == 400
        assert "no room" in refused.json()["error"]
        no_topic = await client.post(
            "/api/v1/sensors", json={"name": "x", "source": "mqtt"}, headers={"x-csrf-token": token}
        )
        assert no_topic.status_code == 400


async def test_the_outdoor_reference(site: Site) -> None:
    async with logged_in(site) as client:
        await form(
            client,
            "/sensors",
            "/sensors",
            name="North wall",
            topic="o/t",
            quantity="temperature",
            placement="outdoor",
        )
        await form(
            client,
            "/sensors",
            "/sensors",
            name="Hall",
            topic="h/t",
            quantity="temperature",
            placement="other",
        )
        wrong = await form(client, "/sensors", "/sensors/outdoor", temperature="hall")
        assert wrong.status_code == 400
        chosen = await form(client, "/sensors", "/sensors/outdoor", temperature="north-wall")
        assert chosen.status_code == 303
        hub = site.services.sensors
        assert hub is not None
        hub.receive_mqtt("o/t", b"-3.5")
        assert site.services.values.latest[Key(SITE, "outdoor/temperature")].value == -3.5
        status = (await client.get("/status/values")).text
        assert "<h2>Outdoors</h2>" in status


async def test_names_replace_built_in_ones(site: Site) -> None:
    async with logged_in(site) as client:
        page = (await client.get("/points/pump/hp1/cs1/supply.temp")).text
        assert 'name="ref" value="pump:hp1/cs1/supply.temp"' in page
        await form(
            client,
            "/points/pump/hp1/cs1/supply.temp",
            "/names",
            ref="pump:hp1/cs1",
            name="Floor heating",
            next="/",
        )
        await form(
            client,
            "/points/pump/hp1/outdoor.temp",
            "/names",
            ref="pump:hp1/outdoor.temp",
            name="Ute",
            next="/",
        )
        status = (await client.get("/status/values")).text
        assert "Floor heating \N{MIDDLE DOT} Supply temperature" in status
        assert ">Ute<" in status
        assert (await site.services.db.get(Names)).names == {  # type: ignore[union-attr]
            "pump:hp1/cs1": "Floor heating",
            "pump:hp1/outdoor.temp": "Ute",
        }
        await form(
            client,
            "/points/pump/hp1/outdoor.temp",
            "/names",
            ref="pump:hp1/outdoor.temp",
            name="",
            next="/",
        )
        assert "Outdoor temperature" in (await client.get("/status/values")).text


async def test_choosing_from_home_assistant(site: Site) -> None:
    fake = FakeHomeAssistant()
    fake.set(
        "sensor.bedroom_temperature",
        "20.5",
        device_class="temperature",
        unit_of_measurement="°C",
        friendly_name="Bedroom temperature",
    )
    fake.set(
        "climate.living_room",
        "heat",
        current_temperature=21.0,
        temperature=21.5,
        hvac_action="idle",
        friendly_name="Living thermostat",
    )
    fake.entity_areas = {"sensor.bedroom_temperature": "bed"}
    await site.services.db.put(Room(name="Bedroom"), "bedroom")
    await site.services.sensors.load()  # type: ignore[union-attr]
    async with running(fake) as url, logged_in(site) as client:
        connected = await form(
            client, "/settings", "/settings/homeassistant", id="homeassistant", url=url, token=TOKEN
        )
        assert connected.headers["location"] == "/sensors/homeassistant/homeassistant"
        page = (await client.get("/sensors/homeassistant/homeassistant")).text
        assert "Bedroom temperature" in page
        assert '<option value="bedroom" selected>Bedroom</option>' in page  # the area's room
        rows = {
            m.group(2): m.group(1) for m in re.finditer(r'name="point_(\d+)" value="([^"]+)"', page)
        }
        n = rows["ha/x.homeassistant.sensor.bedroom_temperature"]
        picked = await client.post(
            "/sensors/homeassistant/homeassistant/sensors",
            data={
                "csrf": csrf_of(page),
                "pick": n,
                f"entity_{n}": "sensor.bedroom_temperature",
                f"point_{n}": "ha/x.homeassistant.sensor.bedroom_temperature",
                f"quantity_{n}": "temperature",
                f"name_{n}": "Bedroom",
                f"room_{n}": "bedroom",
            },
        )
        assert picked.status_code == 303
    plugin = await site.services.db.get(Plugin, "homeassistant")
    assert plugin is not None
    assert plugin.settings["entities"] == ["sensor.bedroom_temperature"]
    stored = await site.services.secrets.get("homeassistant.token")
    assert stored is not None
    sensor = await site.services.db.get(Sensor, "bedroom")
    assert sensor is not None
    assert (sensor.point, sensor.room) == (
        "homeassistant:ha/x.homeassistant.sensor.bedroom_temperature",
        "bedroom",
    )
    assert TOKEN not in (site.state / "audit" / "audit.jsonl").read_text()


async def test_the_mqtt_broker_settings(site: Site) -> None:
    async with logged_in(site) as client:
        saved = await form(
            client,
            "/settings",
            "/settings/mqtt",
            host="192.0.2.30",
            port="1883",
            username="thermaestro",
            password="broker secret",
        )
        assert saved.status_code == 303
        api = (await client.get("/api/v1/mqtt")).json()
        assert api["settings"]["password"] == "mqtt.password"
        assert api["state"] == "off"
        settings = (await client.get("/settings")).text
        assert "broker secret" not in settings
        assert 'action="/settings/mqtt"' in settings
        assert 'action="/settings/mqtt"' not in (await client.get("/sensors")).text
        # Saved again without a password: the one entered stays.
        await form(client, "/settings", "/settings/mqtt", host="192.0.2.31", port="1883")
        api = (await client.get("/api/v1/mqtt")).json()
        assert (api["settings"]["host"], api["settings"]["password"]) == (
            "192.0.2.31",
            "mqtt.password",
        )


async def test_a_viewer_sees_rooms_but_changes_nothing(site: Site) -> None:
    accounts = site.services.accounts
    viewer = await accounts.create_user("vic", ADMIN_PASSWORD + " too", ["Viewers"], by="cli")
    raw = await accounts.start_session(viewer)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=site.app), base_url=ORIGIN
    ) as client:
        client.cookies.set("thermaestro_session", raw)
        assert (await client.get("/api/v1/site")).status_code == 200
        assert (await client.get("/api/v1/sensors")).status_code == 403
        assert (await client.get("/sensors")).status_code == 403
        assert (await client.get("/rooms")).status_code == 403
