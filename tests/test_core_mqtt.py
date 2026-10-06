import asyncio
import contextlib
import socket
from collections.abc import AsyncIterator
from pathlib import Path

import aiomqtt
import pytest
from amqtt.broker import Broker  # type: ignore[import-untyped]

from thermaestro.core.mqtt import MqttInput
from thermaestro.core.sensors import SENSORS, SensorHub
from thermaestro.core.values import Key, Values
from thermaestro.store import Database, Mqtt, Room, SecretStore, Sensor


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port: int = s.getsockname()[1]
        return port


@pytest.fixture
async def broker() -> AsyncIterator[int]:
    port = free_port()
    config = {
        "listeners": {"default": {"type": "tcp", "bind": f"127.0.0.1:{port}"}},
        "plugins": {"amqtt.plugins.authentication.AnonymousAuthPlugin": {"allow_anonymous": True}},
    }
    server = Broker(config)
    await server.start()
    try:
        yield port
    finally:
        await server.shutdown()


async def until(condition: object, timeout: float = 10.0) -> None:
    async with asyncio.timeout(timeout):
        while not condition():  # type: ignore[operator]
            await asyncio.sleep(0.05)


async def test_mqtt_sensors_are_read(broker: int, tmp_path: Path) -> None:
    async with await Database.open(tmp_path / "t.db") as db:
        await db.put(Mqtt(host="127.0.0.1", port=broker))
        await db.put(Room(name="Living room"), "living")
        await db.put(
            Sensor(
                name="Sofa", source="mqtt", topic="z2m/sofa", json_key="temperature", room="living"
            ),
            "sofa",
        )
        values = Values(db)
        hub = SensorHub(db, values)
        await hub.load()
        mqtt = MqttInput(db, SecretStore(tmp_path / "secrets.json"), hub)
        task = asyncio.create_task(mqtt.run())
        try:
            await until(lambda: mqtt.state == "connected")
            async with aiomqtt.Client("127.0.0.1", broker) as client:
                await client.publish("z2m/sofa", b'{"temperature":21.5,"linkquality":80}')
                await until(lambda: Key(SENSORS, "sofa/temperature") in values.latest)
            assert values.latest[Key(SENSORS, "sofa/temperature")].value == 21.5
            # A new sensor: the client starts over with its topic.
            await db.put(Sensor(name="Hall", source="mqtt", topic="t/hall", room="living"), "hall")
            await hub.load()
            mqtt.reload()
            await until(lambda: mqtt.state == "connected" and "t/hall" in hub.topics())
            await asyncio.sleep(0.2)
            async with aiomqtt.Client("127.0.0.1", broker) as client:
                await client.publish("t/hall", b"19.0")
                await until(lambda: Key(SENSORS, "hall/temperature") in values.latest)
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task


async def test_an_unreachable_broker_is_retried(tmp_path: Path) -> None:
    async with await Database.open(tmp_path / "t.db") as db:
        await db.put(Mqtt(host="127.0.0.1", port=free_port()))
        await db.put(Sensor(name="x", source="mqtt", topic="t/x", placement="other"), "x")
        hub = SensorHub(db, Values(db))
        await hub.load()
        mqtt = MqttInput(db, SecretStore(tmp_path / "secrets.json"), hub, backoff_s=(0.05, 0.1))
        task = asyncio.create_task(mqtt.run())
        try:
            await until(lambda: mqtt.state == "failed")
            assert mqtt.error
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task


async def test_no_broker_no_client(tmp_path: Path) -> None:
    async with await Database.open(tmp_path / "t.db") as db:
        hub = SensorHub(db, Values(db))
        await hub.load()
        mqtt = MqttInput(db, SecretStore(tmp_path / "secrets.json"), hub)
        task = asyncio.create_task(mqtt.run())
        await asyncio.sleep(0.1)
        assert mqtt.state == "off"
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
