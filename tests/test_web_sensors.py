"""Sensors, rooms, the outdoor references, names and Home Assistant in the web UI and API."""

import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

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
        rooms = (await client.get("/setup/house")).text
        assert '<option value="pump:hp1/cs1">Climate system 1</option>' in rooms
        made = await form(
            client,
            "/setup/house",
            "/rooms",
            name="Living room",
            climate_system="pump:hp1/cs1",
            own_device="simple_thermostat",
        )
        assert made.status_code == 303
        added = await form(
            client,
            "/setup/sensors",
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
        rooms = (await client.get("/setup/house")).text
        assert "Sofa" in rooms
        assert "turn the thermostat up" in rooms  # the advice for a simple thermostat
        site_api = (await client.get("/api/v1/site")).json()
        assert site_api["rooms"][0]["points"][0]["value"] == 21.5
        # Removing the room keeps the sensor, without a room.
        await form(client, "/setup/house", "/rooms/living-room/delete")
        sensor = await site.services.db.get(Sensor, "sofa")
        assert sensor is not None
        assert sensor.room is None


async def test_bad_sensors_are_refused(site: Site) -> None:
    async with logged_in(site) as client:
        token = csrf_of((await client.get("/setup/sensors")).text)
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
            "/setup/sensors",
            "/sensors",
            name="North wall",
            topic="o/t",
            quantity="temperature",
            placement="outdoor",
        )
        await form(
            client,
            "/setup/sensors",
            "/sensors",
            name="Hall",
            topic="h/t",
            quantity="temperature",
            placement="other",
        )
        wrong = await form(client, "/setup/sensors", "/sensors/outdoor", temperature="hall")
        assert wrong.status_code == 400
        chosen = await form(client, "/setup/sensors", "/sensors/outdoor", temperature="north-wall")
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
        assert re.search(r"<h3>Floor heating</h3>.*Supply temperature", status, re.S)
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
            client,
            "/setup/external",
            "/settings/homeassistant",
            id="homeassistant",
            url=url,
            token=TOKEN,
        )
        assert connected.headers["location"] == "/sensors/homeassistant/homeassistant"
        page = (await client.get("/sensors/homeassistant/homeassistant")).text
        assert "Bedroom temperature" in page
        # First the ticks alone, then names and rooms for what was ticked.
        point = "ha/x.homeassistant.sensor.bedroom_temperature"
        assert f'name="pick" value="{point}"' in page
        page = (
            await client.get("/sensors/homeassistant/homeassistant/pick", params={"pick": point})
        ).text
        assert "Living thermostat" not in page
        assert '<option value="bedroom" selected>Bedroom</option>' in page  # the area's room
        rows = {
            m.group(2): m.group(1) for m in re.finditer(r'name="point_(\d+)" value="([^"]+)"', page)
        }
        n = rows[point]
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


async def test_rooms_from_home_assistants_areas(site: Site) -> None:
    """An area with no room yet is offered as a new room, in the picker and under House;
    a room made so is Thermaestro's own, and the pages say so."""
    fake = FakeHomeAssistant()
    for entity, area in (("sensor.k1", "kitchen"), ("sensor.k2", "kitchen"), ("sensor.b", "bed")):
        fake.set(entity, "20.0", device_class="temperature", unit_of_measurement="°C")
        fake.entity_areas[entity] = area
    fake.set("sensor.h", "20.0", device_class="temperature", unit_of_measurement="°C")
    fake.entity_areas["sensor.h"] = "hall"
    fake.areas = {"kitchen": "Kitchen", "bed": "Bedroom", "hall": "Hall"}
    async with running(fake) as url, logged_in(site) as client:
        await form(
            client,
            "/setup/external",
            "/settings/homeassistant",
            id="homeassistant",
            url=url,
            token=TOKEN,
        )
        points = [f"ha/x.homeassistant.sensor.{e}" for e in ("k1", "k2")]
        page = (
            await client.get("/sensors/homeassistant/homeassistant/pick", params={"pick": points})
        ).text
        assert page.count('<option value="new:Kitchen" selected>New room: Kitchen</option>') == 2
        assert "renaming or removing the area in Home Assistant later" in page
        rows = {
            m.group(2): m.group(1) for m in re.finditer(r'name="point_(\d+)" value="([^"]+)"', page)
        }
        fields: dict[str, Any] = {"csrf": csrf_of(page), "pick": list(rows.values())}
        for point, n in rows.items():
            fields |= {
                f"entity_{n}": point.removeprefix("ha/x.homeassistant."),
                f"point_{n}": point,
                f"quantity_{n}": "temperature",
                f"name_{n}": point[-2:],
                f"room_{n}": "new:Kitchen",
            }
        made = await client.post("/sensors/homeassistant/homeassistant/sensors", data=fields)
        assert made.status_code == 303
        house = (await client.get("/setup/house")).text
        assert 'name="areas" value="Bedroom"' in house  # not rooms yet
        assert 'name="areas" value="Hall"' in house
        assert 'name="areas" value="Kitchen"' not in house  # a room now
        assert "renaming or removing the area in Home Assistant later" in house
        refused = await form(
            client, "/setup/house", "/rooms/from-areas", id="homeassistant", areas="Attic"
        )
        assert refused.status_code == 400
        done = await form(
            client, "/setup/house", "/rooms/from-areas", id="homeassistant", areas="Hall"
        )
        assert done.status_code == 303
        areas = (await client.get("/api/v1/homeassistant/homeassistant/areas")).json()
    assert areas == ["Bedroom"]
    rooms = await site.services.db.all(Room)
    assert sorted(r.name for r in rooms.values()) == ["Hall", "Kitchen"]
    kitchen = next(id for id, r in rooms.items() if r.name == "Kitchen")
    sensors = await site.services.db.all(Sensor)
    assert sorted(s.room or "" for s in sensors.values()) == [kitchen, kitchen]


async def test_a_large_home_assistant_can_be_picked_from(site: Site) -> None:
    """A browser sends every field of a form: the list itself sends only the ticks, so
    hundreds of entities stay under the server's limit of 1000 fields."""
    fake = FakeHomeAssistant()
    for i in range(1200):
        fake.set(
            f"sensor.t{i}",
            "20.0",
            device_class="temperature",
            unit_of_measurement="°C",
            friendly_name=f"Sensor {i}",
        )
    async with running(fake) as url, logged_in(site) as client:
        await form(
            client,
            "/setup/external",
            "/settings/homeassistant",
            id="homeassistant",
            url=url,
            token=TOKEN,
        )
        page = (await client.get("/sensors/homeassistant/homeassistant")).text
        listing = page.split('action="/sensors/homeassistant/homeassistant/pick"', 1)[1]
        listing = listing.split("</form>", 1)[0]
        assert 'method="get"' in page
        fields = set(re.findall(r'<(?:input|select)[^>]* name="([^"]+)"', listing))
        assert fields == {"pick"}
        point = "ha/x.homeassistant.sensor.t1199"
        chosen = (
            await client.get("/sensors/homeassistant/homeassistant/pick", params={"pick": point})
        ).text
        assert "Sensor 1199" in chosen
        assert "Sensor 1198" not in chosen


async def test_the_mqtt_broker_settings(site: Site) -> None:
    async with logged_in(site) as client:
        saved = await form(
            client,
            "/setup/external",
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
        external = (await client.get("/setup/external")).text
        assert "broker secret" not in external
        assert 'action="/settings/mqtt"' in external
        assert 'action="/settings/mqtt"' not in (await client.get("/setup/sensors")).text
        # Saved again without a password: the one entered stays.
        await form(client, "/setup/external", "/settings/mqtt", host="192.0.2.31", port="1883")
        api = (await client.get("/api/v1/mqtt")).json()
        assert (api["settings"]["host"], api["settings"]["password"]) == (
            "192.0.2.31",
            "mqtt.password",
        )


async def test_home_assistant_discovery_settings(site: Site) -> None:
    async with logged_in(site) as client:
        page = (await client.get("/setup/external")).text
        assert 'action="/settings/discovery"' in page
        assert "Set the MQTT broker above first." in page
        off = (await client.get("/api/v1/discovery")).json()
        assert (off["settings"]["enabled"], off["settings"]["id"], off["state"]) == (
            False,
            None,
            "off",
        )
        saved = await form(
            client,
            "/setup/external",
            "/settings/discovery",
            enabled="1",
            language="sv",
            prefix="ha",
        )
        assert saved.status_code == 303
        on = (await client.get("/api/v1/discovery")).json()["settings"]
        assert on["enabled"] is True
        assert re.fullmatch(r"[0-9a-f]{8}", on["id"])  # made when first switched on
        assert (on["language"], on["prefix"], on["base"], on["sensors"]) == (
            "sv",
            "ha",
            "thermaestro",
            False,
        )
        # Off again: the id stays, so what was published can be found and cleared.
        await form(client, "/setup/external", "/settings/discovery", prefix="ha")
        again = (await client.get("/api/v1/discovery")).json()["settings"]
        assert (again["enabled"], again["id"]) == (False, on["id"])
        token = csrf_of((await client.get("/setup/external")).text)
        refused = await client.put(
            "/api/v1/discovery", json={"prefix": "ha/#"}, headers={"x-csrf-token": token}
        )
        assert refused.status_code == 400
    assert '"kind":"discovery"' in (site.state / "audit" / "audit.jsonl").read_text()


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
        assert (await client.get("/setup/sensors")).status_code == 403
        assert (await client.get("/setup/house")).status_code == 403


async def test_pinned_points_get_cards_and_categories_can_move(site: Site) -> None:
    async with logged_in(site) as client:
        page = (await client.get("/points/pump/hp1/dhw/temp.top")).text
        assert "Pinned to the overview" in page
        await form(
            client,
            "/points/pump/hp1/dhw/temp.top",
            "/display",
            ref="pump:hp1/dhw/temp.top",
            pinned="1",
            next="/",
        )
        status = (await client.get("/status/values")).text
        assert "<h2>Hot water</h2>" in status  # a card named after what it belongs to
        assert "Top temperature" in status
        # Moved to the diagnostics: off the overview, onto the pump page's diagnostics.
        await form(
            client,
            "/points/pump/hp1/outdoor.temp",
            "/display",
            ref="pump:hp1/outdoor.temp",
            category="diagnostic",
            next="/",
        )
        status = (await client.get("/status/values")).text
        assert 'title="hp1/outdoor.temp"' not in status
        diagnostics = await client.get("/pump", params={"show": "diagnostic"})
        assert 'title="hp1/outdoor.temp"' in diagnostics.text
        shown = (await client.get("/api/v1/display")).json()
        assert shown == {
            "categories": {"pump:hp1/outdoor.temp": "diagnostic"},
            "pinned": ["pump:hp1/dhw/temp.top"],
        }
        # Unpinned again from the same form.
        await form(
            client,
            "/points/pump/hp1/dhw/temp.top",
            "/display",
            ref="pump:hp1/dhw/temp.top",
            next="/",
        )
        assert "<h2>Hot water</h2>" not in (await client.get("/status/values")).text


async def test_page_order_and_a_broker_first(site: Site) -> None:
    async with logged_in(site) as client:
        house = (await client.get("/setup/house")).text
        assert house.index('href="/setup/house"') < house.index('href="/setup/pump"')
        sensors = (await client.get("/setup/sensors")).text
        assert 'action="/sensors" class="grid"' not in sensors  # no broker: no MQTT sensor form
        assert "Set up the MQTT broker under External" in sensors
        await form(client, "/setup/external", "/settings/mqtt", host="192.0.2.30", port="1883")
        assert 'action="/sensors" class="grid"' in (await client.get("/setup/sensors")).text
        users = (await client.get("/users")).text
        assert users.index("New group") < users.index("What the rights allow")
