from typing import Any

import pytest
from thermaestro_gateway import nibe
from vectors import load, named

V = load("nibe-frames.json")


@named(V["checksum"])
def test_checksum(case: dict[str, Any]) -> None:
    assert nibe.checksum(bytes.fromhex(case["bytes"])) == case["checksum"]


@named(V["replies"])
def test_building_a_request(case: dict[str, Any]) -> None:
    if "value" in case:
        built = nibe.write_request(case["register"], case["value"])
    else:
        built = nibe.read_request(case["register"])
    assert built.hex() == case["hex"]


@named(V["replies"])
def test_valid_reply_passes(case: dict[str, Any]) -> None:
    nibe.validate_reply(bytes.fromhex(case["hex"]))


@named(V["invalid_replies"])
def test_invalid_reply_is_refused(case: dict[str, Any]) -> None:
    with pytest.raises(nibe.FrameError):
        nibe.validate_reply(bytes.fromhex(case["hex"]))


@named(V["telegrams"])
def test_parsing_a_telegram(case: dict[str, Any]) -> None:
    t = nibe.parse_telegram(bytes.fromhex(case["hex"]))
    assert (t.address, t.command, t.payload.hex()) == (
        case["address"],
        case["command"],
        case["payload"],
    )
    assert t.is_token == (case["payload"] == "")


@named(V["invalid_telegrams"])
def test_invalid_telegram_is_refused(case: dict[str, Any]) -> None:
    with pytest.raises(nibe.FrameError):
        nibe.parse_telegram(bytes.fromhex(case["hex"]))


@named(V["exchanges"])
def test_splitting_an_exchange(case: dict[str, Any]) -> None:
    x = nibe.split_exchange(bytes.fromhex(case["hex"]))
    assert (x.telegram.hex(), x.reply.hex(), x.trailer.hex()) == (
        case["telegram"],
        case["reply"],
        case["trailer"],
    )


@pytest.mark.parametrize(("register", "value"), [(-1, 0), (65536, 0), (47387, -1), (47387, 2**32)])
def test_out_of_range_requests_are_refused(register: int, value: int) -> None:
    with pytest.raises(ValueError, match="range"):
        nibe.write_request(register, value)
