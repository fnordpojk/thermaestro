from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from thermaestro.cap.model import Envelope
from thermaestro.core.sensors import (
    SENSORS,
    SITE,
    SensorHub,
    absolute_humidity,
    dew_point,
    parse,
)
from thermaestro.core.values import Key, Values
from thermaestro.store import Database, Outdoor, Room, Sensor


class Clock:
    def __init__(self) -> None:
        self.now = 2_000_000_000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
async def db(tmp_path: Path) -> AsyncIterator[Database]:
    async with await Database.open(tmp_path / "t.db") as db:
        yield db


def site(values: Values, point: str) -> tuple[object, str, str | None]:
    e = values.latest[Key(SITE, point)]
    return e.value, e.quality, e.why


def envelope(point: str, value: float | bool | None, quality: str = "good") -> Envelope:
    now = datetime.now(UTC)
    return Envelope.model_validate(
        {
            "point": point,
            "value": value,
            "unit": "degC" if isinstance(value, float) else None,
            "t_observed": now,
            "t_received": now,
            "quality": quality,
            "source": "measured",
        }
    )


@pytest.mark.parametrize(
    ("payload", "key", "value"),
    [
        (b"21.5", None, 21.5),
        (b" 21 ", None, 21.0),
        (b"ON", None, True),
        (b"closed", None, False),
        (b'{"temperature":21.5,"humidity":40}', "temperature", 21.5),
        (b'{"state":{"temperature":19}}', "state.temperature", 19.0),
        (b'{"contact":false}', "contact", False),
        (b"warm", None, None),
        (b'{"temperature":21.5}', "humidity", None),
        (b"not json", "temperature", None),
    ],
)
def test_payloads(payload: bytes, key: str | None, value: object) -> None:
    assert parse(payload, key) == value


def test_humidity_formulas() -> None:
    assert dew_point(20.0, 50.0) == pytest.approx(9.26, abs=0.05)
    assert dew_point(0.0, 100.0) == pytest.approx(0.0, abs=0.01)
    assert absolute_humidity(20.0, 50.0) == pytest.approx(8.65, abs=0.05)


async def test_a_room_is_the_mean_of_its_sensors(db: Database) -> None:
    await db.put(Room(name="Living room", climate_system="pump:hp1/cs1"), "living")
    await db.put(
        Sensor(name="Sofa", source="mqtt", topic="z2m/sofa", json_key="temperature", room="living"),
        "sofa",
    )
    await db.put(
        Sensor(name="Shelf", source="point", point="ha:sensor.shelf", room="living"), "shelf"
    )
    await db.put(
        Sensor(
            name="Sofa humidity",
            source="mqtt",
            topic="z2m/sofa",
            json_key="humidity",
            quantity="humidity",
            room="living",
        ),
        "sofa-rh",
    )
    values = Values(db)
    hub = SensorHub(db, values)
    await hub.load()
    assert site(values, "room.living/temperature") == (None, "unknown", "no value yet")
    hub.receive_mqtt("z2m/sofa", b'{"temperature":21.0,"humidity":50}')
    values.add("ha", envelope("sensor.shelf", 22.0))
    assert site(values, "room.living/temperature") == (21.5, "good", "mean of 2 sensors")
    assert site(values, "room.living/humidity") == (50.0, "good", None)
    dew, quality, _ = site(values, "room.living/dew_point")
    assert quality == "good"
    assert dew == pytest.approx(dew_point(21.5, 50.0), abs=0.01)
    # Each sensor's own value is kept too.
    assert values.latest[Key(SENSORS, "sofa/temperature")].value == 21.0
    # A sensor that goes bad drops out of the mean.
    values.add("ha", envelope("sensor.shelf", None, "unknown"))
    assert site(values, "room.living/temperature") == (21.0, "good", None)


async def test_a_reference_sensor_wins(db: Database) -> None:
    await db.put(Room(name="Bedroom"), "bed")
    await db.put(Sensor(name="Window", source="point", point="ha:a", room="bed"), "a")
    await db.put(
        Sensor(name="Inner wall", source="point", point="ha:b", room="bed", reference=True), "b"
    )
    values = Values(db)
    hub = SensorHub(db, values)
    await hub.load()
    values.add("ha", envelope("a", 17.0))
    values.add("ha", envelope("b", 20.0))
    assert site(values, "room.bed/temperature") == (20.0, "good", None)


async def test_calibration_and_staleness(db: Database) -> None:
    clock = Clock()
    await db.put(Room(name="Hall"), "hall")
    await db.put(
        Sensor(
            name="Hall",
            source="mqtt",
            topic="t/hall",
            room="hall",
            calibration_offset=-0.5,
            freshness_s=600,
        ),
        "hall",
    )
    values = Values(db)
    hub = SensorHub(db, values, clock=clock)
    await hub.load()
    hub.receive_mqtt("t/hall", b"20.5")
    assert values.latest[Key(SENSORS, "hall/temperature")].value == 20.0
    clock.now += 601
    hub.tick()
    sensor = values.latest[Key(SENSORS, "hall/temperature")]
    assert (sensor.quality, sensor.why) == ("stale", "no report for 10 min 1 s")
    assert site(values, "room.hall/temperature") == (None, "unknown", "no sensor with a good value")
    hub.receive_mqtt("t/hall", b"garbage")
    assert values.latest[Key(SENSORS, "hall/temperature")].quality == "unknown"


async def test_windows_are_open_if_any_is(db: Database) -> None:
    await db.put(Room(name="Kitchen"), "kitchen")
    for id in ("w1", "w2"):
        await db.put(
            Sensor(name=id, source="mqtt", topic=f"w/{id}", quantity="window.open", room="kitchen"),
            id,
        )
    values = Values(db)
    hub = SensorHub(db, values)
    await hub.load()
    hub.receive_mqtt("w/w1", b"closed")
    hub.receive_mqtt("w/w2", b"open")
    assert site(values, "room.kitchen/window.open")[:2] == (True, "good")


async def test_the_outdoor_reference(db: Database) -> None:
    values = Values(db)
    hub = SensorHub(db, values)
    await hub.load()
    # Without a choice, the pump's own sensor.
    values.add("pump", envelope("hp1/outdoor.temp", 4.5))
    assert site(values, "outdoor/temperature")[:2] == (4.5, "good")
    await db.put(
        Sensor(name="North wall", source="mqtt", topic="o/t", placement="outdoor"), "north"
    )
    await db.put(
        Sensor(
            name="North wall RH",
            source="mqtt",
            topic="o/rh",
            quantity="humidity",
            placement="outdoor",
        ),
        "north-rh",
    )
    await db.put(Outdoor(references={"temperature": "north", "humidity": "north-rh"}))
    await hub.load()
    hub.receive_mqtt("o/t", b"3.0")
    hub.receive_mqtt("o/rh", b"80")
    assert site(values, "outdoor/temperature")[:2] == (3.0, "good")
    assert site(values, "outdoor/dew_point")[1] == "good"
    assert site(values, "outdoor/absolute_humidity")[0] == pytest.approx(
        absolute_humidity(3.0, 80.0), abs=0.01
    )


def test_room_sensors_belong_to_rooms() -> None:
    with pytest.raises(ValueError, match="only a room sensor"):
        Sensor(name="x", source="mqtt", topic="t", placement="outdoor", room="living")
