"""Reading NibePi's config.json into a draft: every line, the edge cases, and the report."""

import json

import pytest
from nibepi_configs import (
    BROKER_PASSWORD,
    GATEWAY,
    TIBBER_TOKEN,
    changed,
    line_1_0,
    line_1_1,
    line_1_2_1,
    line_ours,
    line_pizzi,
)

from thermaestro.migrate.nibepi import NotNibePi, _flat, read
from thermaestro.store import Location, Mqtt, NibeGateway, Room, Sensor


def kinds(draft: object) -> dict[str, dict[str, object]]:
    return {f"{i.kind}:{i.id}": i.body for i in draft.items}  # type: ignore[attr-defined]


def test_the_consolidated_fork() -> None:
    draft = read(json.dumps(line_ours()))
    assert draft.line == "the consolidated fork"
    found = kinds(draft)
    assert NibeGateway.model_validate(found["pump:pump"]) == NibeGateway(
        host=GATEWAY, read_port=10000, write_port=10001
    )
    assert Mqtt.model_validate(found["mqtt:"]).password == "mqtt.password"
    assert draft.secrets == {"mqtt.password": BROKER_PASSWORD}
    assert draft.localhost_broker
    assert found["discovery:"] == {"enabled": False}
    assert Location.model_validate(found["location:"]).timezone == "Europe/Stockholm"
    assert found["spot:SE3"] == {"zone": "SE3"}
    # The living room is named by two features of system 1, the office by system 2; the
    # hall by none, so it has no room; the pump's own register isn't a sensor to make.
    rooms = {k: Room.model_validate(v) for k, v in found.items() if k.startswith("room:")}
    assert {r.name: r.climate_system for r in rooms.values()} == {
        "Living room": "pump:hp1/cs1",
        "Office": "pump:hp1/cs2",
    }
    sensors = [Sensor.model_validate(v) for k, v in found.items() if k.startswith("sensor:")]
    assert {s.name: s.room for s in sensors} == {
        "Living room": "living-room",
        "Office": "office",
        "Hall": None,
    }
    assert all(s.freshness_s is None for s in sensors)  # 0 in NibePi: learned instead
    assert any("Pump BT50" in n for n in draft.notes)
    assert (draft.offsets, draft.before) == ({1: -1.0}, {})


def test_every_key_is_in_the_report_and_no_secret_anywhere() -> None:
    for config in (line_ours(), line_pizzi(), line_1_2_1(), line_1_1()):
        draft = read(json.dumps(config))
        reported = {r.key for r in draft.rows}
        assert reported == set(_flat(config))
        assert all(r.outcome in ("carried", "translated", "left_out") for r in draft.rows)
        shown = json.dumps(
            [
                [r.note for r in draft.rows],
                draft.notes,
                [i.body for i in draft.items],
                [i.what for i in draft.items],
            ]
        )
        for secret in (BROKER_PASSWORD, TIBBER_TOKEN, "plejd-secret", "an-old-cloud-token"):
            assert secret not in shown


def test_modbus_tcp_with_string_ports_and_no_model() -> None:
    draft = read(json.dumps(line_1_2_1()))
    assert draft.line == "1.2.1 or Åhsberg's"
    pump = NibeGateway.model_validate(
        {**kinds(draft)["pump:pump"], "model": "S1255"}  # the household chooses it
    )
    assert (pump.protocol, pump.host, pump.modbus_port) == ("modbus-tcp", "192.0.2.30", 502)
    assert "model" not in kinds(draft)["pump:pump"]
    assert any("needs its model" in n for n in draft.notes)
    assert Mqtt.model_validate(kinds(draft)["mqtt:"]).port == 1884
    rows = {r.key: r for r in draft.rows}
    assert rows["price.token"].outcome == "left_out"
    assert rows["tcp.server"].outcome == "left_out"
    assert rows["plejd.pass"].outcome == "left_out"
    # The fees NibePi had are offered, unticked.
    layers = [i for i in draft.items if i.kind == "layer"]
    assert {(i.id, i.body["value"], i.optional) for i in layers} == {
        ("tax.energy", 0.45, True),
        ("grid.transfer", 0.25, True),
    }


def test_tibber_for_another_home_and_vat() -> None:
    draft = read(json.dumps(line_pizzi()))
    assert draft.line == "the VV-AI line or the consolidated fork"
    assert kinds(draft)["tibber:tibber"] == {"token": "tibber.token", "home": None}
    assert draft.secrets["tibber.token"] == TIBBER_TOKEN
    assert any("home 2" in n for n in draft.notes)
    vat = next(i for i in draft.items if i.kind == "vat")
    assert (vat.body, vat.optional) == ({"rate": 0.25}, True)
    # The hot-water period NibePi's learning saved before holding it at 0.
    assert draft.before == {47134: 30}
    assert {r.key: r.outcome for r in draft.rows}["hotwater.vv_backup_hw_period"] == "translated"


def test_a_serial_pump_and_a_buffer_offset() -> None:
    draft = read(json.dumps(line_1_1()))
    assert draft.line == "1.1"
    assert "pump:pump" not in kinds(draft)
    assert any("thermaestro-gateway" in n for n in draft.notes)
    rows = {r.key: r for r in draft.rows}
    # Set over MQTT, NibePi kept the raw payload, "1": offered in the settings review.
    assert rows["home.adjust_s1"].outcome == "translated"
    assert draft.offsets == {1: 1.0}
    assert "location:" not in kinds(draft)  # no lat/lon in this file


def test_the_pumps_smart_price_and_an_unknown_area() -> None:
    config = changed(line_ours(), price={"source": "nibe", "area": "Oslo"})
    draft = read(json.dumps(config))
    rows = {r.key: r for r in draft.rows}
    assert rows["price.source"].outcome == "left_out"
    assert "isn't a bidding zone" in rows["price.area"].note
    assert any("Smart Price Adaption" in n for n in draft.notes)
    assert Location.model_validate(kinds(draft)["location:"]).timezone == "UTC"


@pytest.mark.parametrize(
    ("text", "why"),
    [
        (json.dumps(line_1_0()), "NibePi 1.0"),
        ("not json", "isn't a JSON file"),
        (json.dumps([1, 2]), "isn't a NibePi config.json"),
        (json.dumps({"name": "something else"}), "isn't a NibePi config.json"),
    ],
)
def test_what_isnt_read(text: str, why: str) -> None:
    with pytest.raises(NotNibePi, match=why):
        read(text)
