import dataclasses
from enum import IntEnum
from typing import Any

import pytest
from thermaestro_gateway import protocol as p
from vectors import load, named

V = load("thermaestro-gw.json")
HEADER_FIELDS = {"id", "gw_time_us", "options"}


def as_json(decoded: p.Decoded) -> dict[str, Any]:
    """The vector file's description of a decoded datagram."""
    msg = decoded.message
    core: dict[str, Any] = {}
    for f in dataclasses.fields(msg):
        if f.name in HEADER_FIELDS:
            continue
        value = getattr(msg, f.name)
        core[f.name] = value.hex() if isinstance(value, bytes) else int(value)
    h = decoded.header
    return {
        "type": msg.TYPE.name,
        "ver_major": h.ver_major,
        "ver_minor": h.ver_minor,
        "flags": h.flags,
        "id": msg.id,
        "gw_time_us": msg.gw_time_us,
        "core": core,
        "options": [{"tag": o.tag, "value": o.value.hex()} for o in msg.options],
    }


def from_json(description: dict[str, Any]) -> p.Message:
    cls = p.MESSAGE_CLASSES[p.MessageType[description["type"]]]
    kwargs: dict[str, Any] = {}
    for f in dataclasses.fields(cls):
        if f.name in HEADER_FIELDS:
            continue
        value = description["core"][f.name]
        kwargs[f.name] = bytes.fromhex(value) if f.type in (bytes, "bytes") else value
    options = tuple(p.Option(o["tag"], bytes.fromhex(o["value"])) for o in description["options"])
    return cls(
        id=description["id"], gw_time_us=description["gw_time_us"], options=options, **kwargs
    )


@named(V["messages"])
def test_decoding(case: dict[str, Any]) -> None:
    assert as_json(p.decode(bytes.fromhex(case["hex"]))) == case["message"]


@named(V["messages"])
def test_encoding(case: dict[str, Any]) -> None:
    assert p.encode(from_json(case["message"])).hex() == case["hex"]


@named(V["invalid"])
def test_invalid_datagram_is_refused(case: dict[str, Any]) -> None:
    with pytest.raises(p.ProtocolError) as refused:
        p.decode(bytes.fromhex(case["hex"]))
    assert refused.value.code == p.ErrorCode[case["error"].upper()]
    if "tag" in case:
        assert refused.value.tag == case["tag"]


def test_session_key() -> None:
    a = V["auth"]
    key = p.session_key(
        bytes.fromhex(a["psk"]),
        bytes.fromhex(a["client_nonce"]),
        bytes.fromhex(a["gateway_nonce"]),
        a["boot_id"],
    )
    assert key.hex() == a["session_key"]


@named(V["auth"]["signed"])
def test_signed_message_verifies_and_reencodes(case: dict[str, Any]) -> None:
    key = bytes.fromhex(V["auth"][case["key"]])
    datagram = bytes.fromhex(case["hex"])
    decoded = p.decode(datagram)
    assert decoded.header.flags & p.FLAG_AUTHENTICATED
    assert p.verify(datagram, key) == case["seq"]
    assert p.encode(decoded.message, key=key, seq=case["seq"]) == datagram


@named(V["auth"]["bad"])
def test_wrong_mac_is_refused(case: dict[str, Any]) -> None:
    with pytest.raises(p.ProtocolError) as refused:
        p.verify(bytes.fromhex(case["hex"]), bytes.fromhex(V["auth"][case["key"]]))
    assert refused.value.code == p.ErrorCode[case["error"].upper()]


def test_unsigned_datagram_doesnt_verify() -> None:
    hello = bytes.fromhex(V["messages"][0]["hex"])
    with pytest.raises(p.ProtocolError) as refused:
        p.verify(hello, b"k" * 32)
    assert refused.value.code == p.ErrorCode.AUTH_REQUIRED


def test_option_helpers_round_trip() -> None:
    assert p.Option.u16(p.Tag.LEASE_S, 120).as_int() == 120
    assert p.Option.text(p.Tag.CLIENT_NAME, "thermaestro").as_text() == "thermaestro"
    assert p.Option.i8(p.Tag.WIFI_RSSI, -61).as_int() == -61
    assert p.Option(p.Tag.CLIENT_NONCE | p.CRITICAL, b"").critical


def test_finding_options() -> None:
    msg = p.decode(bytes.fromhex(V["messages"][0]["hex"])).message
    lease = p.find(msg.options, p.Tag.LEASE_S)
    assert lease is not None
    assert lease.as_int() == 120
    assert p.find(msg.options, p.Tag.CLIENT_NONCE) is None


def test_options_that_overflow_a_datagram_are_refused() -> None:
    big = p.Health(id=1, options=(p.Option(0x7F00, b"x" * p.MAX_DATAGRAM),))
    with pytest.raises(ValueError, match="512"):
        p.encode(big)


def test_enums_are_int_enums() -> None:
    for enum in (p.MessageType, p.Stage, p.DropReason, p.AnswerStatus, p.ErrorCode, p.Tag):
        assert issubclass(enum, IntEnum)
