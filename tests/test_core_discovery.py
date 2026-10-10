"""Home Assistant discovery against a broker in the test: what is published, that each
message passes Home Assistant's own checks, and that everything is cleared again."""

import asyncio
import contextlib
import json
import re
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import aiomqtt
import pytest
from test_core_mqtt import broker, until  # noqa: F401 - the fixture

from thermaestro.cap.messages import Described
from thermaestro.cap.model import (
    Delivery,
    Envelope,
    Identity,
    Knowledge,
    Node,
    Point,
    Presence,
    Provider,
    Terms,
)
from thermaestro.core.discovery import Publisher, classify, key
from thermaestro.core.host import Instance, PluginHost, State
from thermaestro.core.mqtt import MqttClient
from thermaestro.core.sensors import SensorHub
from thermaestro.core.series import Series
from thermaestro.core.values import Values
from thermaestro.store import (
    Database,
    Discovery,
    Location,
    Mqtt,
    Plugin,
    PriceLayer,
    SecretStore,
)

ID = "abcd1234"
ME = f"thermaestro_{ID}"
OURS = ("homeassistant/device/+/config", f"thermaestro/{ID}/#")

# Home Assistant's own checks on an MQTT discovery message, restated from its source
# (components/mqtt: schemas.py, sensor.py, binary_sensor.py, util.py; components/sensor:
# const.py), for the device classes Thermaestro uses.
HA_UNITS = {
    "temperature": {"°C", "°F", "K"},
    "humidity": {"%"},
    "absolute_humidity": {"g/m³", "mg/m³"},
    "atmospheric_pressure": {"cbar", "bar", "hPa", "mmHg", "inHg", "kPa", "mbar", "Pa", "psi"},
    "illuminance": {"lx"},
    "irradiance": {"W/m²", "BTU/(h⋅ft²)"},
    "power": {"mW", "W", "kW", "MW", "GW", "TW"},
    "energy": {"J", "kJ", "MJ", "GJ", "mWh", "Wh", "kWh", "MWh", "GWh", "TWh", "cal", "kcal"},
    "volume_flow_rate": {"m³/h", "m³/min", "m³/s", "ft³/min", "L/h", "L/min", "L/s", "gal/min"},
    "frequency": {"Hz", "kHz", "MHz", "GHz", "mHz"},
    "current": {"A", "mA"},
    "voltage": {"V", "mV", "µV", "μV", "kV", "MV"},
    "duration": {"d", "h", "min", "s", "ms", "μs"},
    "carbon_dioxide": {"ppm"},
    "volatile_organic_compounds": {"μg/m³", "mg/m³"},
    "volatile_organic_compounds_parts": {"ppb", "ppm"},
    "pm25": {"μg/m³"},
    "pm10": {"μg/m³"},
    "water": {"CCF", "ft³", "m³", "gal", "L", "MCF"},
}
BINARY_CLASSES = {"window", "opening", "door", "presence", "occupancy", "motion", "moisture"}
BINARY_CLASSES |= {"problem", "running"}
OBJECT_ID = re.compile(r"^[a-zA-Z0-9_-]+$")


def check_ha(topic: str, message: dict[str, Any], devices: set[str]) -> None:
    """Fail as Home Assistant would refuse the message."""
    match = re.fullmatch(r"homeassistant/device/([^/]+)/config", topic)
    assert match, topic
    assert OBJECT_ID.match(match.group(1)), topic
    assert message["device"]["identifiers"], "a device has an identifier"
    assert message["origin"]["name"], "device discovery requires the origin"
    if "via_device" in message["device"]:
        assert message["device"]["via_device"] in devices
    assert message["components"]
    for component in message["components"].values():
        platform = component["platform"]
        assert platform in ("sensor", "binary_sensor", "button")
        assert component["unique_id"], "an entity component has a unique id"
        if platform == "button":
            assert "state_topic" not in component
            assert not set("#+") & set(component["command_topic"])
            assert json.loads(component["payload_press"])["kind"]
            continue
        assert not set("#+") & set(component["state_topic"])
        assert component.get("entity_category") != "config", "sensors can't be config"
        device_class = component.get("device_class")
        unit = component.get("unit_of_measurement")
        if platform == "binary_sensor":
            assert device_class is None or device_class in BINARY_CLASSES
            assert unit is None
            continue
        if "options" in component:
            assert device_class == "enum"
            assert component["options"]
            assert "state_class" not in component
            assert unit is None
        elif device_class is not None:
            assert unit in HA_UNITS[device_class], (device_class, unit)


def point(path: str, unit: str | None = None, **more: Any) -> Point:
    return Point(path=path, unit=unit, delivery=Delivery(how="polled", cost_s=1.0), **more)


def envelope(path: str, value: object, unit: str | None = None, quality: str = "good") -> Envelope:
    return Envelope.model_validate(
        {
            "point": path,
            "value": value,
            "unit": unit if isinstance(value, float) else None,
            "t_observed": datetime.now(UTC),
            "t_received": datetime.now(UTC),
            "quality": quality,
            "source": "measured",
        }
    )


PUMP_POINTS = (
    point("hp1/outdoor.temp", "degC", resolution=Knowledge(value=0.1, known="documented")),
    point(
        "hp1/heat.produced{purpose=heating,by=total}",
        "kWh",
        wraps_at=429496729.6,
    ),
    point(
        "hp1/demand",
        enum=Knowledge(value={"idle": 0, "heating": 1, "dhw": 2}, known="documented"),
    ),
    point("hp1/x.nibe.47011", "degC", label="Offset", category="config"),
    point("hp1/x.nibe.48852", label="Word swap", category="diagnostic"),
)


def pump(points: tuple[Point, ...] = PUMP_POINTS) -> Instance:
    node = Node(
        path="hp1",
        kind="unit",
        presence=Presence(how="configured"),
        label="F1245-10 PC",
        identity=Identity(vendor="Nibe", model="F1245", firmware="9682"),
    )
    return Instance(
        "pump",
        Plugin(plugin="nibe", settings={"host": "192.0.2.10"}),
        state=State.UP,
        described=Described(nodes=(node,), points=points),
    )


def home_assistant() -> Instance:
    node = Node(
        path="ha",
        kind="unit",
        presence=Presence(how="configured"),
        identity=Identity(vendor="Home Assistant", model="2026.10"),
    )
    return Instance(
        "ha",
        Plugin(
            plugin="homeassistant",
            settings={"url": "http://192.0.2.20:8123", "token": "ha.token"},
        ),
        state=State.UP,
        described=Described(
            nodes=(node,), points=(point("ha/x.homeassistant.sensor.porch", "degC"),)
        ),
    )


class Plain:
    """Names without a language: enough to see where each comes from."""

    def point(self, instance: str, path: str, language: str) -> str:
        return f"{instance}:{path}"

    def node(self, instance: str, path: str) -> str | None:
        return None

    def category(self, instance: str, path: str, given: str | None) -> str | None:
        return given

    def text(self, what: str, language: str, **values: object) -> str:
        return f"{what} {values}" if values else what

    def part(self, instance: str, path: str, language: str) -> str:
        return path

    def lever(self, path: str, language: str) -> str:
        return path


async def retained(port: int, wait: float = 0.4) -> dict[str, bytes]:
    """The retained messages under this installation's topics, as a new client sees them."""
    found: dict[str, bytes] = {}
    async with aiomqtt.Client("127.0.0.1", port) as client:
        for pattern in OURS:
            await client.subscribe(pattern)
        with contextlib.suppress(TimeoutError):
            async with asyncio.timeout(wait):
                async for message in client.messages:
                    if message.retain and message.payload:
                        found[str(message.topic)] = bytes(message.payload)
    return {
        t: p
        for t, p in found.items()
        if t.startswith(("thermaestro/", f"homeassistant/device/{ME}"))
    }


class Listener:
    """Everything published under this installation's topics from now on."""

    def __init__(self) -> None:
        self.messages: list[tuple[str, bytes]] = []

    def configs(self, object_id: str) -> list[dict[str, Any]]:
        topic = f"homeassistant/device/{object_id}/config"
        return [json.loads(p) for t, p in self.messages if t == topic and p]

    def last(self, topic: str) -> bytes | None:
        found = [p for t, p in self.messages if t == topic]
        return found[-1] if found else None


@contextlib.asynccontextmanager
async def listening(port: int) -> AsyncIterator[Listener]:
    listener = Listener()
    async with aiomqtt.Client("127.0.0.1", port) as client:
        for pattern in OURS:
            await client.subscribe(pattern)

        async def read() -> None:
            async for message in client.messages:
                listener.messages.append((str(message.topic), bytes(message.payload)))

        task = asyncio.create_task(read())
        try:
            yield listener
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task


@contextlib.asynccontextmanager
async def publishing(
    tmp_path: Path, port: int, host: Any
) -> AsyncIterator[tuple[Database, Values, Publisher, MqttClient]]:
    async with await Database.open(tmp_path / "t.db") as db:
        await db.put(Mqtt(host="127.0.0.1", port=port))
        await db.put(Discovery(enabled=True, id=ID))
        values = Values(db)
        hub = SensorHub(db, values)
        await hub.load()
        client = MqttClient(db, SecretStore(tmp_path / "secrets.json"), hub)
        publisher = Publisher(
            db,
            values,
            cast(PluginHost, host),
            hub,
            Series(db),
            client,
            min_interval_s=0.0,
            rebuild_s=0.1,
            sweep_s=0.3,
            birth_delay_s=(0.0, 0.0),
        )
        task = asyncio.create_task(client.run())
        try:
            yield db, values, publisher, client
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task


def entity(message: dict[str, Any], ref: str) -> dict[str, Any]:
    found: dict[str, Any] = message["components"][key(*ref.split(":", 1))]
    return found


async def test_thermaestro_and_the_pump_appear_as_devices(broker: int, tmp_path: Path) -> None:  # noqa: F811
    pump_device = f"{ME}_{key('pump', 'hp1')}"
    # What an earlier run left behind: a device and a value no longer published.
    async with aiomqtt.Client("127.0.0.1", broker) as c:
        await c.publish(f"homeassistant/device/{ME}_gone/config", b'{"device":{}}', retain=True)
        await c.publish(f"thermaestro/{ID}/state/old_123456", b"1", retain=True)
        await c.publish("homeassistant/device/someone_else/config", b"{}", retain=True)
    host = SimpleNamespace(instances={"pump": pump(), "ha": home_assistant()})
    async with publishing(tmp_path, broker, host) as (db, values, publisher, client):
        values.add("site", envelope("room.living/temperature", 21.25, "degC"))
        values.add("site", envelope("room.living/window.open", False))
        values.add("site", envelope("outdoor/humidity", 81.0, "%"))
        for p, v in zip(PUMP_POINTS, (-3.5, 12345.0, "heating", -1.0, True), strict=True):
            values.add("pump", envelope(p.path, v, p.unit), p)
        values.add("ha", envelope("ha/x.homeassistant.sensor.porch", 4.0, "degC"))
        async with listening(broker) as heard:
            publisher.start(Plain())
            await until(
                lambda: heard.configs(pump_device) and heard.last(f"thermaestro/{ID}/status")
            )
            await asyncio.sleep(0.5)
            found = await retained(broker)

            # Home Assistant would take both, and nothing of Home Assistant's own comes back.
            mine = json.loads(found[f"homeassistant/device/{ME}/config"])
            theirs = json.loads(found[f"homeassistant/device/{pump_device}/config"])
            devices = {ME, pump_device}
            check_ha(f"homeassistant/device/{ME}/config", mine, devices)
            check_ha(f"homeassistant/device/{pump_device}/config", theirs, devices)
            assert sorted(t for t in found if "/device/" in t) == sorted(
                f"homeassistant/device/{d}/config" for d in devices
            )
            assert found[f"thermaestro/{ID}/status"] == b"online"
            assert not any("old_123456" in t for t in found)

            assert theirs["device"]["name"] == "Nibe F1245-10 PC"
            assert theirs["device"]["model"] == "F1245"
            assert theirs["device"]["via_device"] == ME
            assert theirs["device"]["sw_version"] == "9682"
            outdoor = entity(theirs, "pump:hp1/outdoor.temp")
            assert (outdoor["device_class"], outdoor["unit_of_measurement"]) == (
                "temperature",
                "°C",
            )
            assert outdoor["suggested_display_precision"] == 1
            assert found[outdoor["state_topic"]] == b"-3.5"
            heat = entity(theirs, "pump:hp1/heat.produced{purpose=heating,by=total}")
            assert (heat["device_class"], heat["state_class"]) == ("energy", "total_increasing")
            demand = entity(theirs, "pump:hp1/demand")
            assert demand["options"] == ["dhw", "heating", "idle"]
            assert found[demand["state_topic"]] == b"heating"
            offset = entity(theirs, "pump:hp1/x.nibe.47011")
            assert offset["entity_category"] == "diagnostic"
            assert offset["enabled_by_default"] is False

            room = entity(mine, "site:room.living/temperature")
            assert room["name"] == "site:room.living/temperature"
            assert found[room["state_topic"]] == b"21.25"
            window = entity(mine, "site:room.living/window.open")
            assert (window["platform"], window["device_class"]) == ("binary_sensor", "window")
            assert found[window["state_topic"]] == b"OFF"
            humidity = entity(mine, "site:outdoor/humidity")
            assert humidity["device_class"] == "humidity"
            status = mine["components"][key("status", "pump")]
            assert found[status["state_topic"]] == b"up"
            assert key("status", "ha") in mine["components"]

            # A new value goes out; one that isn't good is unknown.
            values.add("site", envelope("room.living/temperature", 22.0, "degC"))
            await until(lambda: heard.last(room["state_topic"]) == b"22.0")
            values.add("site", envelope("room.living/temperature", None, quality="unknown"))
            await until(lambda: heard.last(room["state_topic"]) == b"None")

            # Home Assistant's birth message: everything again.
            before = len(heard.configs(ME))
            async with aiomqtt.Client("127.0.0.1", broker) as ha:
                await ha.publish("homeassistant/status", b"online")
            await until(lambda: len(heard.configs(ME)) > before)

            # A point the pump no longer has: deleted in Home Assistant, then left out.
            gone = key("pump", "hp1/x.nibe.48852")
            host.instances["pump"] = pump(PUMP_POINTS[:-1])
            await until(lambda: gone not in heard.configs(pump_device)[-1]["components"])
            removal = heard.configs(pump_device)[-2]["components"][gone]
            assert removal == {"platform": "binary_sensor"}

            # Switched off: everything is cleared, and the client disconnects.
            before_setting = await db.get(Discovery)
            off = Discovery(enabled=False, id=ID)
            await db.put(off)
            publisher.changed(before_setting, off)
            await until(lambda: publisher.state == "off" and client.state == "off")
        assert await retained(broker) == {}


async def test_sensors_only_when_asked(broker: int, tmp_path: Path) -> None:  # noqa: F811
    host = SimpleNamespace(instances={})
    async with publishing(tmp_path, broker, host) as (db, values, publisher, _):
        values.add("sensors", envelope("sofa/temperature", 21.0, "degC"))
        values.add("site", envelope("room.living/temperature", 21.0, "degC"))
        async with listening(broker) as heard:
            publisher.start(Plain())
            await until(lambda: heard.configs(ME))
            assert key("sensors", "sofa/temperature") not in heard.configs(ME)[-1]["components"]
            before = await db.get(Discovery)
            asked = Discovery(enabled=True, id=ID, sensors=True)
            await db.put(asked)
            publisher.changed(before, asked)
            await until(
                lambda: key("sensors", "sofa/temperature") in heard.configs(ME)[-1]["components"]
            )


async def test_the_price_now_with_its_sources_credited(broker: int, tmp_path: Path) -> None:  # noqa: F811
    terms = Terms(
        attribution=Knowledge(value="Source: ENTSO-E Transparency Platform", known="documented")
    )
    entsoe = Instance(
        "entsoe",
        Plugin(plugin="entsoe", settings={"token": "entsoe.token", "zone": "SE3"}),
        state=State.UP,
        described=Described(provider=Provider(name="ENTSO-E", terms=terms)),
    )
    host = SimpleNamespace(instances={"entsoe": entsoe})
    async with publishing(tmp_path, broker, host) as (db, _, publisher, _):
        await db.put(Location(latitude=59.33, longitude=18.07, timezone="Europe/Stockholm"))
        fixed = {"source": "fixed", "unit": "SEK/kWh", "vat": "excl"}
        await db.put(PriceLayer(role="energy.supplier", value=0.5, **fixed), "adder")
        await db.put(PriceLayer(role="grid.energy", value=0.25, **fixed), "grid")
        async with listening(broker) as heard:
            publisher.start(Plain())
            price = key("thermaestro", "price")
            await until(lambda: heard.last(f"thermaestro/{ID}/attributes/{price}"))
            component = heard.configs(ME)[-1]["components"][price]
            assert component["unit_of_measurement"] == "SEK/kWh"
            assert heard.last(component["state_topic"]) == b"0.75"
            attributes = json.loads(heard.last(component["json_attributes_topic"]) or b"")
            assert len(attributes["today"]) in (92, 96, 100)
            assert attributes["today"][0]["price"] == 0.75
            assert "attribution" not in attributes  # no series from ENTSO-E in the stack
            assert (
                publisher._attribution(
                    [
                        PriceLayer(
                            role="energy.spot",
                            source="series",
                            plugin="entsoe",
                            series="spot",
                            unit="SEK/kWh",
                            vat="excl",
                        )
                    ]
                )
                == "Source: ENTSO-E Transparency Platform"
            )


@pytest.mark.parametrize(
    ("name", "unit", "counter", "expected"),
    [
        ("hp1/cs1/supply.temp", "degC", False, ("temperature", "measurement")),
        ("room.x/humidity", "%", False, ("humidity", "measurement")),
        ("hp1/x.nibe.43437", "%", False, (None, "measurement")),
        ("room.x/carbon_dioxide", "ppm", False, ("carbon_dioxide", "measurement")),
        ("hp1/elec.used", "kWh", False, ("energy", "total_increasing")),
        ("hp1/x.nibe.43416", None, True, (None, "total_increasing")),
        ("hp1/x.nibe.43420", "h", False, ("duration", None)),
        ("hp1/degree_minutes", "degC.min", False, (None, "measurement")),
    ],
)
def test_device_and_state_classes(
    name: str, unit: str | None, counter: bool, expected: tuple[str | None, str | None]
) -> None:
    assert classify(name, unit, counter) == expected


async def test_requests_over_mqtt(broker: int, tmp_path: Path) -> None:  # noqa: F811
    unit = Node(
        path="hp1",
        kind="unit",
        presence=Presence(how="configured"),
        identity=Identity(vendor="Nibe", model="F1245"),
    )
    system = Node(path="hp1/cs1", kind="climate_system", presence=Presence(how="configured"))
    tank = Node(path="hp1/dhw", kind="dhw_tank", presence=Presence(how="configured"))
    instance = Instance(
        "pump",
        Plugin(plugin="nibe", settings={"host": "192.0.2.10"}),
        state=State.UP,
        described=Described(nodes=(unit, system, tank), points=PUMP_POINTS),
    )
    asked: list[dict[str, Any]] = []

    async def asker(body: dict[str, Any]) -> dict[str, Any]:
        asked.append(body)
        return {"accepted": True, "id": "in-1", "messages": ["Boost now: until done"]}

    async def control() -> dict[str, Any]:
        return {
            "intents": [{"kind": "boost_now", "scope": "pump:hp1/dhw", "end": None, "by": "mqtt"}],
            "deadline": {
                "t": "2026-10-10T19:30:00+00:00",
                "at_least": 50.0,
                "scope": "pump:hp1/dhw",
            },
            "modes": {},
            "last": None,
        }

    host = SimpleNamespace(instances={"pump": instance})
    async with publishing(tmp_path, broker, host) as (_, values, publisher, _):
        publisher.asker, publisher.control = asker, control
        for p, v in zip(PUMP_POINTS, (-3.5, 12345.0, "heating", -1.0, True), strict=True):
            values.add("pump", envelope(p.path, v, p.unit), p)
        async with listening(broker) as heard:
            publisher.start(Plain())
            await until(
                lambda: heard.last(f"thermaestro/{ID}/state/{key('thermaestro', 'intents')}")
            )
            devices = {ME, f"{ME}_{key('pump', 'hp1')}"}
            pump_config = heard.configs(f"{ME}_{key('pump', 'hp1')}")[-1]
            check_ha(f"homeassistant/device/{ME}_{key('pump', 'hp1')}/config", pump_config, devices)
            boost = pump_config["components"][key("request", "boost", "pump:hp1/dhw")]
            assert boost["command_topic"] == f"thermaestro/{ID}/request"
            assert json.loads(boost["payload_press"]) == {
                "kind": "boost_now",
                "scope": "pump:hp1/dhw",
            }
            warmer = pump_config["components"][key("request", "warmer", "pump:hp1/cs1")]
            assert json.loads(warmer["payload_press"])["offset"] == 1
            assert heard.last(f"thermaestro/{ID}/state/{key('thermaestro', 'intents')}") == b"1"
            assert heard.last(f"thermaestro/{ID}/state/{key('thermaestro', 'deadline')}") == (
                b"2026-10-10T19:30:00+00:00"
            )
            async with aiomqtt.Client("127.0.0.1", broker) as client:
                await client.subscribe(f"thermaestro/{ID}/request/result")
                await client.publish(f"thermaestro/{ID}/request", boost["payload_press"])
                async with asyncio.timeout(5):
                    async for message in client.messages:
                        if str(message.topic) == f"thermaestro/{ID}/request/result":
                            answer = json.loads(bytes(message.payload))
                            break
            assert asked == [{"kind": "boost_now", "scope": "pump:hp1/dhw"}]
            assert answer["accepted"]
            # A retained request isn't acted on: it would be asked again at every connection.
            async with aiomqtt.Client("127.0.0.1", broker) as client:
                await client.publish(
                    f"thermaestro/{ID}/request", b'{"kind": "fireplace"}', retain=True
                )
                await client.publish(f"thermaestro/{ID}/request", b"", retain=True)
            await asyncio.sleep(0.5)
            assert len(asked) == 1
