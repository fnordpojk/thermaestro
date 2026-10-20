from pathlib import Path

import pytest
from thermaestro_gateway import nibe
from thermaestro_gateway.__main__ import parse


def test_defaults() -> None:
    config, level = parse(["/dev/ttyUSB0"])
    assert config.serial_port == "/dev/ttyUSB0"
    assert (config.read_port, config.write_port, config.control_port) == (9999, 10000, 10090)
    assert config.acknowledged == frozenset({nibe.MODBUS40})
    assert config.constants == {
        (nibe.MODBUS40, nibe.ACCESSORY_TOKEN): nibe.MODBUS40_ACCESSORY_REPLY
    }
    assert config.psk is None
    assert config.sources is None
    assert level == "INFO"


def test_ports_can_be_turned_off_and_constants_replaced() -> None:
    config, _ = parse(
        [
            "/dev/serial0",
            "--write-port",
            "0",
            "--no-default-constants",
            "--constant",
            "0x19:0x63:0600",
            "--source",
            "192.0.2.5",
            "--target",
            "192.0.2.6:9999",
        ]
    )
    assert config.write_port is None
    assert config.constants == {(0x19, 0x63): b"\x06\x00"}
    assert config.sources == frozenset({"192.0.2.5"})
    assert config.static_targets == (("192.0.2.6", 9999),)


def test_a_psk_file(tmp_path: Path) -> None:
    key = tmp_path / "psk"
    key.write_text(bytes(range(32)).hex() + "\n")
    config, _ = parse(["/dev/ttyUSB0", "--psk-file", str(key)])
    assert config.psk == bytes(range(32))


def test_a_short_psk_is_refused(tmp_path: Path) -> None:
    key = tmp_path / "psk"
    key.write_text("abcd")
    with pytest.raises(SystemExit):
        parse(["/dev/ttyUSB0", "--psk-file", str(key)])


def test_a_key_turns_the_plain_write_port_off(tmp_path: Path) -> None:
    """Else the key would guard the control port while anyone writes through the plain one;
    asked for, it opens again."""
    key = tmp_path / "psk"
    key.write_text(bytes(range(32)).hex())
    config, _ = parse(["/dev/ttyUSB0", "--psk-file", str(key)])
    assert (config.read_port, config.write_port, config.control_port) == (9999, None, 10090)
    assert not config.takes_plain_writes
    opened, _ = parse(["/dev/ttyUSB0", "--psk-file", str(key), "--write-port", "10000"])
    assert (opened.write_port, opened.takes_plain_writes) == (10000, True)
    plain, _ = parse(["/dev/ttyUSB0"])
    assert plain.takes_plain_writes
