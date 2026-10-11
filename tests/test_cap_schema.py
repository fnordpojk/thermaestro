"""The checked-in JSON Schema: current, and it validates an example of every message."""

import json
from typing import Any

import pytest
from jsonschema import Draft202012Validator

from thermaestro.cap import MESSAGES
from thermaestro.cap.messages import TYPES
from thermaestro.cap.schema import dumps, generate, shipped

T = "2026-09-29T22:28:04.120Z"

ENVELOPE = {
    "point": "hp1/dhw/temp.charge",
    "value": 47.3,
    "unit": "degC",
    "raw": 473,
    "t_observed": "2026-09-29T22:28:04.120Z",
    "t_received": "2026-09-29T22:28:04.180Z",
    "quality": "good",
    "source": "measured",
    "resolution": 0.1,
    "why": None,
}

RANGE = {
    "value": {"min": 5.0, "max": 70.0, "step": 0.1},
    "known": "documented",
    "basis": "the vendor's register database",
}

SERIES = {
    "id": "example/home/price.import",
    "kind": "price",
    "role": "energy.supplier",
    "covers": {
        "value": ["energy.spot", "energy.supplier", "vat"],
        "known": "verified",
        "basis": ["the supplier's API reference", "checked against its totals"],
    },
    "unit": "SEK/kWh",
    "vat": "incl",
    "resolution": "PT15M",
    "area": "SE3",
    "publication": {"daily_after": "13:00", "tz": "Europe/Stockholm"},
}

INTERVAL = {
    "series": "example/SE3/spot",
    "start": "2026-09-29T00:00:00+02:00",
    "end": "2026-09-29T00:15:00+02:00",
    "value": 0.59016,
    "unit": "SEK/kWh",
    "vat": "excl",
    "status": "final",
    "revision": 1,
    "t_published": "2026-09-28T11:10:00Z",
    "source": "derived",
    "why": "a EUR price at the day's exchange rate",
}

RULE = {
    "type": "interval_peak",
    "owner": "Example Grid AB",
    "status": "paused",
    "valid_from": "2026-09-01",
    "parameters": {
        "window": {
            "value": {"months": [11, 12, 1, 2, 3], "hours": "06:00-20:59"},
            "known": "documented",
            "basis": "the grid company's tariff page",
        },
        "price_per_kw": {"known": "unknown"},
    },
}

NODES = [
    {
        "path": "hp1",
        "kind": "unit",
        "presence": {"how": "configured"},
        "identity": {"vendor": "Nibe", "model": "F1245-6", "map": "nibe-bus-F1245"},
        "transport": {
            "fate": "best_effort",
            "ack": {"means": "accepted", "detail": "0x6C: 1 accepted, 0 refused"},
            "sees_other_writers": {"value": "yes", "known": "reported"},
            "budget": {"known": "unknown"},
        },
    },
    {
        "path": "hp1/cs2",
        "kind": "climate_system",
        "presence": {"how": "detected", "rule": "47302 = 1 and 40007 isn't 0x8000"},
    },
    {"path": "hp1/dhw", "kind": "dhw_tank", "presence": {"how": "assumed"}},
]

LEVER = {
    "path": "hp1/dhw/block",
    "kind": "hold",
    "works": {"value": True, "known": "verified", "basis": "a tank test"},
    "persistence": {"value": {"kind": "stored"}, "known": "verified"},
    "wear": {"value": {"kind": "flash", "per_call": 2}, "known": "documented"},
    "implementation": {
        "kind": "emulated",
        "how": "the current mode's start temperature lowered to 25.0 °C",
        "side_effects": ["two setting writes per cycle", "a floor at 25.0 °C"],
    },
    "verify": {"kind": "effect", "point": "hp1/dhw/temp.charge", "expectation": "no charge"},
    "competing_features": [{"name": "the pump's hot-water schedule"}],
    "touches": ["x.nibe.47043", "x.nibe.47044", "x.nibe.47045"],
}

POINT = {
    "path": "hp1/dhw/temp.charge",
    "unit": "degC",
    "resolution": {"value": 0.1, "known": "documented"},
    "range": RANGE,
    "delivery": {"how": "pushed", "interval_s": 0.5},
    "validity": ["transitional while a charge starts"],
}

EXAMPLES: list[dict[str, Any]] = [
    {"type": "hello", "id": 1, "protocol": "thermaestro-cap", "version": "0.1", "role": "core"},
    {
        "type": "hello",
        "id": 1,
        "protocol": "thermaestro-cap",
        "version": "0.1",
        "role": "plugin",
        "plugin": "nibe",
        "plugin_version": "0.1.0",
        "features": ["subscribe", "fate.best_effort", "sees_other_writers"],
    },
    {"type": "describe", "id": 2},
    {
        "type": "described",
        "id": 2,
        "nodes": NODES,
        "points": [POINT],
        "levers": [LEVER],
        "series": [SERIES],
    },
    {"type": "described", "complete": False, "removed": ["hp1/cs2"]},
    {
        "type": "read",
        "id": 7,
        "points": ["hp1/dhw/temp.top", "hp1/demand"],
        "after": "2026-09-29T22:34:10.000Z",
    },
    {"type": "values", "id": 7, "values": [ENVELOPE]},
    {"type": "subscribe", "id": 8, "points": ["hp1/outdoor.temp"], "min_interval_s": 60},
    {"type": "unsubscribe", "id": 8},
    {"type": "update", "id": 8, "values": [ENVELOPE]},
    {"type": "act", "id": 42, "lever": "hp1/dhw/block", "op": "engage", "params": {}},
    {
        "type": "act",
        "id": 43,
        "lever": "hp1/cs1/heating.offset",
        "op": "set",
        "params": {"value": -2},
    },
    {"type": "write", "id": 44, "point": "hp1/x.nibe.47134", "value": 30},
    {"type": "fate", "id": 42, "stage": "queued", "t": T},
    {"type": "fate", "id": 42, "stage": "device_accepted", "t": T, "detail": "0x6C = 1"},
    {
        "type": "health",
        "t": T,
        "unit": "hp1",
        "state": "up",
        "last_traffic": T,
        "counters": {"crc": 0, "nak": 2},
        "queue_depth": 1,
        "silence_s": 0.4,
    },
    {"type": "health", "t": T, "state": "contended", "needs_user_action": "reauthorize"},
    {"type": "device_event", "t": T, "unit": "hp1", "code": "251", "text": "Communication error"},
    {"type": "foreign_write", "t": T, "unit": "hp1", "datapoint": "x.nibe.47387", "value": 0},
    {
        "type": "series.get",
        "id": 9,
        "series": "example/SE3/spot",
        "from": "2026-09-29T00:00:00+02:00",
        "to": "2026-09-30T00:00:00+02:00",
    },
    {
        "type": "series.data",
        "id": 9,
        "series": "example/SE3/spot",
        "intervals": [INTERVAL],
        "known_until": "2026-09-30T00:00:00+02:00",
    },
    {"type": "series.subscribe", "id": 10, "series": "example/SE3/spot"},
    {"type": "series.update", "id": 10, "series": "example/SE3/spot", "intervals": [INTERVAL]},
    {"type": "rules.get", "id": 11, "scope": "site"},
    {"type": "rules", "id": 11, "rules": [RULE]},
    {"type": "error", "id": 9, "code": "unsupported", "detail": "no series in this plugin"},
    {"type": "error", "id": None, "code": "invalid", "detail": "not JSON"},
    {"type": "auth", "token": "5f2b9c"},
]

SCHEMA = json.loads(shipped())
VALIDATOR = Draft202012Validator(SCHEMA)


def test_the_checked_in_schema_is_current() -> None:
    assert shipped() == dumps(generate()), "run scripts/make-capschema.py"


def test_the_schema_is_a_valid_schema() -> None:
    Draft202012Validator.check_schema(SCHEMA)


def test_every_message_type_has_an_example() -> None:
    assert {e["type"] for e in EXAMPLES} == TYPES


@pytest.mark.parametrize("example", EXAMPLES, ids=lambda e: str(e["type"]))
def test_the_schema_validates_the_example(example: dict[str, Any]) -> None:
    VALIDATOR.validate(example)


@pytest.mark.parametrize("example", EXAMPLES, ids=lambda e: str(e["type"]))
def test_every_message_round_trips_through_json(example: dict[str, Any]) -> None:
    message = MESSAGES.validate_python(example)
    again = MESSAGES.validate_json(MESSAGES.dump_json(message))
    assert again == message
    VALIDATOR.validate(json.loads(MESSAGES.dump_json(message)))


@pytest.mark.parametrize(
    ("definition", "fragment"),
    [("Envelope", ENVELOPE), ("SeriesInfo", SERIES), ("Interval", INTERVAL)],
)
def test_parts_validate_against_their_definitions(
    definition: str, fragment: dict[str, Any]
) -> None:
    part = {"$ref": f"#/$defs/{definition}", "$defs": SCHEMA["$defs"]}
    Draft202012Validator(part).validate(fragment)


@pytest.mark.parametrize(
    "bad",
    [
        {"type": "hello", "id": 1, "protocol": "thermaestro-cap", "role": "core"},
        {"type": "read", "id": 1, "points": []},
        {"type": "read", "id": 1, "points": ["hp1 has spaces"]},
        {"type": "act", "id": 1, "lever": "hp1/dhw/block", "op": "toggle"},
        {"type": "fate", "id": 1, "stage": "applied", "t": T},
        {"type": "write", "id": 1, "point": "hp1/x.nibe.47134", "value": "thirty"},
        {"type": "describe", "id": -1},
        {"type": "describe", "id": 1, "extra": True},
        {"type": "values", "id": 1, "values": [ENVELOPE | {"quality": "fine"}]},
    ],
)
def test_the_schema_refuses(bad: dict[str, Any]) -> None:
    assert not VALIDATOR.is_valid(bad)
