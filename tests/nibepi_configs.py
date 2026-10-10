"""Simulated NibePi `config.json` files, one per line, from the keys each line's code reads
and writes; every value is made up (documentation addresses, neutral topics, dummy
secrets)."""

import copy
from typing import Any

GATEWAY = "192.0.2.17"
BROKER_PASSWORD = "not-a-real-password"
TIBBER_TOKEN = "not-a-real-tibber-token"


def line_1_1() -> dict[str, Any]:
    """1.1: a serial pump, no `tcp` or `update` section, a Buffer-typed manual offset."""
    return {
        "version": "1.1",
        "connection": {"enable": "serial", "series": "fSeries"},
        "serial": {"port": "/dev/ttyAMA0"},
        "mqtt": {
            "enable": False,
            "host": "127.0.0.1",
            "port": "1883",
            "user": "",
            "pass": "",
            "topic": "nibe/modbus/",
        },
        "home": {
            "adjust_s1": {"type": "Buffer", "data": [49]},
            "inside_sensors": [],
        },
        "price": {"enable": False, "source": "tibber", "token": ""},
        "system": {"readonly": True, "pump": "F1245"},
        "registers": [40004, "40008"],
    }


def line_1_2_1() -> dict[str, Any]:
    """1.2.1: an S-series pump over Modbus TCP (string port, `tcp.pump` "null"), prices for an
    area, the cloud-era token."""
    return {
        "version": "1.2",
        "update": {"version": "1.2.1"},
        "connection": {"enable": "tcp", "series": "sSeries"},
        "serial": {"port": "/dev/ttyAMA0"},
        "tcp": {"host": "192.0.2.30", "port": "502", "pump": "null", "server": "192.0.2.99"},
        "mqtt": {
            "enable": True,
            "host": "192.0.2.40",
            "port": "1884",
            "user": "nibe",
            "pass": BROKER_PASSWORD,
            "topic": "nibe/modbus/",
            "discovery": True,
        },
        "home": {"lat": "59.33", "lon": "18.06", "sensor_timeout": 30, "inside_sensors": []},
        "price": {
            "enable": True,
            "source": "priceai",
            "token": "an-old-cloud-token",
            "area": "SE3",
            "time": 8,
            "min_spread": 20,
            "enable_heat_s1": True,
            "heat_cheap_s1": 2,
            "prio_tax": 45,
            "prio_transfer": 25,
        },
        "indoor": {"enable_s1": False},
        "plejd": {"host": "127.0.0.1", "pass": "plejd-secret"},
    }


def line_pizzi() -> dict[str, Any]:
    """The VV-AI line: Tibber prices for the second home, VAT and markup, VV-AI keys."""
    config = line_1_2_1()
    config["connection"] = {"enable": "nibegw", "series": "fSeries"}
    config["serial"] = {"port": GATEWAY}
    del config["tcp"]
    config["price"] = {
        "enable": True,
        "source": "tibber",
        "token": TIBBER_TOKEN,
        "tibber_home": 1,
        "area": "SE3",
        "vat": 0.25,
        "addition_ore": 88.01,
        "apply_vat": True,
    }
    config["hotwater"] = {
        "enable_vv_learning": True,
        "vv_ai_min_temp": 40,
        "vv_backup_hw_period": 30,
    }
    return config


def line_ours() -> dict[str, Any]:
    """The consolidated fork: a broker on localhost, three room sensors, one of them used by
    two features of system 1, one by system 2, one by none; a pump register as a sensor."""
    return {
        "version": "1.2.1",
        "update": {"version": "2.0.0"},
        "connection": {"enable": "nibegw", "series": "fSeries"},
        "serial": {"port": GATEWAY},
        "mqtt": {
            "enable": True,
            "host": "localhost",
            "port": "1883",
            "user": "home",
            "pass": BROKER_PASSWORD,
            "topic": "nibe/modbus/",
            "discovery": False,
        },
        "home": {
            "lat": "57.70",
            "lon": "11.97",
            "sensor_timeout": 0,
            "adjust_s1": -1,
            "inside_sensors": [
                {"name": "Living room", "register": "house/living/temperature", "source": "mqtt"},
                {"name": "Office", "register": "house/office/temperature", "source": "mqtt"},
                {"name": "Hall", "register": "house/hall/temperature", "source": "mqtt"},
                {"name": "Pump BT50", "register": "40033", "source": "nibe"},
            ],
        },
        "indoor": {"enable_s1": True, "sensor_s1": "Living room", "sensor_s2": "Ingen"},
        "price": {"enable": True, "source": "", "area": "SE3", "sensor_s1": "Living room"},
        "weather": {"enable_s2": True, "sensor_s2": "Office"},
        "hotwater": {"enable_hw_priority": True},
        "log": {"enable": False, "hotwater": False},
        "system": {"docker": True, "id": "0123456789"},
        "registers": [40004, "40008", 43086],
    }


def line_1_0() -> dict[str, Any]:
    return {"plugins": {"tibber": {}}, "defaultTopic": "nibe/", "mqtt": {"active": True}}


def changed(config: dict[str, Any], **sections: dict[str, Any]) -> dict[str, Any]:
    """A copy with sections replaced."""
    out = copy.deepcopy(config)
    out.update(sections)
    return out
