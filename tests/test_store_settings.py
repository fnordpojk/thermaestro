from pathlib import Path

import pytest
from pydantic import ValidationError

from thermaestro.store import (
    Database,
    Location,
    Mqtt,
    NibeGateway,
    Plugin,
    PriceLayer,
    SecretStore,
    Sensor,
    Transaction,
    Vat,
)


def test_a_plugins_settings_are_checked_against_its_model() -> None:
    nibe = Plugin(plugin="nibe", settings={"host": "192.0.2.10", "read_port": 10000})
    assert nibe.typed(NibeGateway).write_port == 10000
    with pytest.raises(ValidationError, match=r"settings\.read_port: "):
        Plugin(plugin="nibe", settings={"host": "192.0.2.10", "read_port": 0})
    with pytest.raises(ValidationError, match="pre-shared key"):
        Plugin(plugin="nibe", settings={"host": "192.0.2.10", "protocol": "thermaestro-gw"})
    # A plugin without a model here keeps its settings as given.
    assert Plugin(plugin="other", settings={"x": [1, 2]}).settings == {"x": [1, 2]}


@pytest.mark.parametrize(
    "bad",
    [
        {"name": "t", "source": "mqtt"},
        {"name": "t", "source": "point", "topic": "a/b"},
        {"name": "t", "source": "mqtt", "topic": "a/b", "freshness_s": 0},
    ],
)
def test_a_sensor_names_its_source(bad: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        Sensor.model_validate(bad)


def test_an_unknown_time_zone_is_refused() -> None:
    with pytest.raises(ValidationError, match="unknown time zone"):
        Location(latitude=0, longitude=0, timezone="Europe/Atlantis")


def test_price_layers_say_where_their_value_comes_from() -> None:
    with pytest.raises(ValidationError, match="names the plugin"):
        PriceLayer(role="energy.spot", source="series", unit="SEK/kWh", vat="excl")
    with pytest.raises(ValidationError, match="gives its value"):
        PriceLayer(role="tax.energy", source="fixed", unit="SEK/kWh", vat="excl")


async def test_an_import_draft_fills_the_settings_in_one_transaction(tmp_path: Path) -> None:
    """What a NibePi import brings, with invented values: the gateway (NibePi's fixed
    ports), the broker and its password, two room sensors for one climate system, the
    location, and Tibber with its token."""
    secrets = SecretStore(tmp_path / "secrets.json")
    await secrets.set("mqtt.password", "invented-password")
    await secrets.set("tibber.token", "invented-token")

    def apply(t: Transaction) -> None:
        t.put(
            Plugin(
                plugin="nibe",
                settings={"host": "192.0.2.10", "read_port": 10000, "write_port": 10001},
            ),
            "pump",
        )
        t.put(Plugin(plugin="tibber", settings={"token": "tibber.token"}), "tibber")
        t.put(Mqtt(host="192.0.2.20", username="nibepi", password="mqtt.password"))
        for id, topic in (("living", "home/living/temperature"), ("hall", "home/hall/temperature")):
            t.put(Sensor(name=id, source="mqtt", topic=topic, room="living"), id)
        t.put(Location(latitude=52.52, longitude=13.40, timezone="Europe/Berlin"))
        t.put(
            PriceLayer(
                role="energy.spot",
                source="series",
                plugin="tibber",
                series="tibber/home/energy",
                unit="SEK/kWh",
                vat="excl",
            ),
            "spot",
        )
        t.put(Vat(rate=0.25, applies_to=("energy.spot", "tax.energy")))

    async with await Database.open(tmp_path / "t.db") as db:
        await db.run(apply)
        sensors = await db.all(Sensor)
        assert [s.room for s in sensors.values()] == ["living", "living"]
        pump = await db.get(Plugin, "pump")
        assert pump is not None
        assert (pump.typed(NibeGateway).read_port, pump.typed(NibeGateway).write_port) == (
            10000,
            10001,
        )
        mqtt = await db.get(Mqtt)
        assert mqtt is not None
        assert mqtt.password is not None
        password = await secrets.get(mqtt.password)
        assert password is not None
        assert password.get_secret_value() == "invented-password"
