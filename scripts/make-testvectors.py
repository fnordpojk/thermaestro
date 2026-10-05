#!/usr/bin/env python3
"""Generate the shared test vectors for the Thermaestro gateway protocol and Nibe bus framing.

Every byte here is built with struct and hmac directly, not with the codec under test, so
the vectors check the codec rather than mirror it. The Python gateway and esphome-nibe
both test against the JSON this writes.

    uv run python scripts/make-testvectors.py           # rewrite the files
    uv run python scripts/make-testvectors.py --check   # fail if they're out of date
"""

import argparse
import hashlib
import hmac
import json
import struct
import sys
from pathlib import Path
from typing import Any

OUT = Path(__file__).resolve().parent.parent / "testvectors"

# --- Nibe bus framing (docs/gateway-protocol.md §2) -----------------------------------------------


def nibe_checksum(data: bytes) -> int:
    x = 0
    for b in data:
        x ^= b
    # 0x5C is the start byte, so the pump sends 0xC5 where the XOR comes out as 0x5C.
    return 0xC5 if x == 0x5C else x


def reply(command: int, data: bytes) -> bytes:
    """An accessory's reply: C0 CMD LEN DATA CHK, checksum over everything before it."""
    head = bytes([0xC0, command, len(data)]) + data
    return head + bytes([nibe_checksum(head)])


def telegram(address: int, command: int, raw_data: bytes) -> bytes:
    """A pump telegram: 5C ADDR_HI ADDR_LO CMD LEN DATA CHK. LEN counts the bytes as sent
    (a doubled 0x5C counts twice); the checksum covers ADDR..DATA as sent."""
    body = struct.pack(">HBB", address, command, len(raw_data)) + raw_data
    return b"\x5c" + body + bytes([nibe_checksum(body)])


def nibe_vectors() -> dict[str, Any]:
    read_47134 = reply(0x69, struct.pack("<H", 47134))
    read_40043 = reply(0x69, struct.pack("<H", 40043))
    write_47387 = reply(0x6B, struct.pack("<HI", 47387, 1))
    token_read = telegram(0x0020, 0x69, b"")
    token_write = telegram(0x0020, 0x6B, b"")
    answer = telegram(0x0020, 0x6A, struct.pack("<HHH", 47134, 45, 0))
    answer_escaped = telegram(0x0020, 0x6A, bytes.fromhex("1eb85c5c000000"))
    write_result = telegram(0x0020, 0x6C, b"\x01")
    rmu_token = telegram(0x0019, 0x60, b"")
    # Worked out by hand, so a slip in the helpers above can't hide.
    for frame, expected in ((read_47134, 0x0D), (read_40043, 0xC5), (token_read, 0x49)):
        if frame[-1] != expected:
            raise SystemExit(f"checksum of {frame.hex()} isn't {expected:#04x}")
    return {
        "note": (
            "Nibe MODBUS40 bus framing; see docs/gateway-protocol.md §2. "
            "Hex strings are bytes as on the wire."
        ),
        "checksum": [
            {"name": "read request 47134", "bytes": read_47134[:-1].hex(), "checksum": 0x0D},
            {
                "name": "XOR is 0x5C, so 0xC5 is sent",
                "bytes": read_40043[:-1].hex(),
                "checksum": 0xC5,
            },
            {"name": "read token telegram", "bytes": token_read[1:-1].hex(), "checksum": 0x49},
        ],
        "replies": [
            {"name": "read 47134", "register": 47134, "hex": read_47134.hex()},
            {"name": "read 40043 (checksum 0xC5)", "register": 40043, "hex": read_40043.hex()},
            {"name": "write 47387 = 1", "register": 47387, "value": 1, "hex": write_47387.hex()},
        ],
        "invalid_replies": [
            {"name": "wrong start byte", "hex": "c169021eb80d"},
            {"name": "bad checksum", "hex": "c069021eb80e"},
            {"name": "XOR 0x5C sent as 0x5C", "hex": read_40043[:-1].hex() + "5c"},
            {"name": "LEN says 3, two data bytes", "hex": "c069031eb80d"},
            {"name": "too short", "hex": "c069"},
        ],
        "telegrams": [
            {
                "name": "read token to MODBUS40",
                "hex": token_read.hex(),
                "address": 0x0020,
                "command": 0x69,
                "payload": "",
            },
            {
                "name": "write token to MODBUS40",
                "hex": token_write.hex(),
                "address": 0x0020,
                "command": 0x6B,
                "payload": "",
            },
            {
                "name": "read answer 47134 = 45 (next register 0)",
                "hex": answer.hex(),
                "address": 0x0020,
                "command": 0x6A,
                "payload": struct.pack("<HHH", 47134, 45, 0).hex(),
            },
            {
                "name": "read answer with a doubled 0x5C in the data",
                "hex": answer_escaped.hex(),
                "address": 0x0020,
                "command": 0x6A,
                "payload": "1eb85c000000",
            },
            {
                "name": "write result: accepted",
                "hex": write_result.hex(),
                "address": 0x0020,
                "command": 0x6C,
                "payload": "01",
            },
            {
                "name": "RMU40 S1 write token",
                "hex": rmu_token.hex(),
                "address": 0x0019,
                "command": 0x60,
                "payload": "",
            },
        ],
        "invalid_telegrams": [
            {"name": "bad checksum", "hex": token_read[:-1].hex() + "48"},
            {"name": "wrong start byte", "hex": "5d" + token_read[1:].hex()},
            {"name": "truncated", "hex": answer[:-3].hex()},
        ],
        "exchanges": [
            {
                "name": "read token answered with a request, pump ACKs",
                "hex": (token_read + read_47134 + b"\x06").hex(),
                "telegram": token_read.hex(),
                "reply": read_47134.hex(),
                "trailer": "06",
            },
            {
                "name": "read token, queue empty, gateway ACKs",
                "hex": (token_read + b"\x06").hex(),
                "telegram": token_read.hex(),
                "reply": "",
                "trailer": "06",
            },
            {
                "name": "write token answered with a write, pump NAKs",
                "hex": (token_write + write_47387 + b"\x15").hex(),
                "telegram": token_write.hex(),
                "reply": write_47387.hex(),
                "trailer": "15",
            },
            {
                "name": "data telegram, gateway ACKs",
                "hex": (answer + b"\x06").hex(),
                "telegram": answer.hex(),
                "reply": "",
                "trailer": "06",
            },
            {
                "name": "reply with no ACK seen (next start byte came)",
                "hex": (token_read + read_47134).hex(),
                "telegram": token_read.hex(),
                "reply": read_47134.hex(),
                "trailer": "",
            },
        ],
    }


# --- The Thermaestro gateway protocol (docs/gateway-protocol.md) ---------------------------------

VER_MAJOR = 0
VER_MINOR = 1
CRITICAL = 0x8000

HELLO, KEEPALIVE, BYE, SUBSCRIBE, REQUEST, CANCEL = 0x01, 0x02, 0x03, 0x04, 0x10, 0x11
WELCOME, FATE, ANSWER, FRAME, HEALTH, ERROR = 0x81, 0x90, 0x91, 0xA0, 0xB0, 0xE0
TYPE_NAMES = {
    HELLO: "HELLO",
    KEEPALIVE: "KEEPALIVE",
    BYE: "BYE",
    SUBSCRIBE: "SUBSCRIBE",
    REQUEST: "REQUEST",
    CANCEL: "CANCEL",
    WELCOME: "WELCOME",
    FATE: "FATE",
    ANSWER: "ANSWER",
    FRAME: "FRAME",
    HEALTH: "HEALTH",
    ERROR: "ERROR",
}


def opt(tag: int, value: bytes) -> bytes:
    return struct.pack("<HH", tag, len(value)) + value


def header(
    msg_type: int,
    body: bytes,
    msg_id: int,
    gw_time_us: int = 0,
    flags: int = 0,
    ver_major: int = VER_MAJOR,
) -> bytes:
    return struct.pack(
        "<2sBBBBHIQ", b"TG", ver_major, VER_MINOR, msg_type, flags, len(body), msg_id, gw_time_us
    )


def message(
    msg_type: int,
    core: bytes,
    options: list[tuple[int, bytes]],
    msg_id: int,
    gw_time_us: int = 0,
) -> bytes:
    body = core + b"".join(opt(t, v) for t, v in options)
    return header(msg_type, body, msg_id, gw_time_us) + body


def described(
    name: str,
    msg_type: int,
    core_fields: dict[str, Any],
    core: bytes,
    options: list[tuple[int, bytes]],
    msg_id: int,
    gw_time_us: int = 0,
) -> dict[str, Any]:
    return {
        "name": name,
        "hex": message(msg_type, core, options, msg_id, gw_time_us).hex(),
        "message": {
            "type": TYPE_NAMES[msg_type],
            "ver_major": VER_MAJOR,
            "ver_minor": VER_MINOR,
            "flags": 0,
            "id": msg_id,
            "gw_time_us": gw_time_us,
            "core": core_fields,
            "options": [{"tag": t, "value": v.hex()} for t, v in options],
        },
    }


def gw_vectors() -> dict[str, Any]:
    read_47134 = reply(0x69, struct.pack("<H", 47134))
    write_47387 = reply(0x6B, struct.pack("<HI", 47387, 1))
    token_read = telegram(0x0020, 0x69, b"")
    answer = telegram(0x0020, 0x6A, struct.pack("<HHH", 47134, 45, 0))
    exchange = token_read + read_47134 + b"\x06"
    nonce_c = bytes(range(0x10, 0x20))

    messages = [
        described(
            "HELLO with name, subscription, lease and health interval",
            HELLO,
            {"ver_min": 0, "ver_max": 0},
            struct.pack("<BB", 0, 0),
            [
                (0x0001, b"thermaestro"),
                (0x0003, struct.pack("<I", 0b101)),
                (0x0004, struct.pack("<H", 120)),
                (0x0005, struct.pack("<H", 10)),
            ],
            msg_id=1,
        ),
        described(
            "HELLO insisting on authentication (critical CLIENT_NONCE)",
            HELLO,
            {"ver_min": 0, "ver_max": 0},
            struct.pack("<BB", 0, 0),
            [(0x0002 | CRITICAL, nonce_c)],
            msg_id=2,
        ),
        described(
            "HELLO with an unknown option that isn't critical",
            HELLO,
            {"ver_min": 0, "ver_max": 0},
            struct.pack("<BB", 0, 0),
            [(0x0042, b"\x01\x02")],
            msg_id=3,
        ),
        described("KEEPALIVE", KEEPALIVE, {}, b"", [], msg_id=7),
        described("BYE", BYE, {}, b"", [], msg_id=8),
        described(
            "SUBSCRIBE to own frames only",
            SUBSCRIBE,
            {},
            b"",
            [(0x0003, struct.pack("<I", 0b010))],
            msg_id=9,
        ),
        described(
            "REQUEST: read 47134, expect an answer, ttl 10 s",
            REQUEST,
            {
                "address": 0x0020,
                "token": 0x69,
                "flags": 0x02,
                "ttl_ms": 10000,
                "answer_timeout_ms": 0,
                "frame": read_47134.hex(),
            },
            struct.pack("<HBBHHB", 0x0020, 0x69, 0x02, 10000, 0, len(read_47134)) + read_47134,
            [],
            msg_id=42,
        ),
        described(
            "REQUEST: write 47387 = 1, priority and expect an answer",
            REQUEST,
            {
                "address": 0x0020,
                "token": 0x6B,
                "flags": 0x03,
                "ttl_ms": 5000,
                "answer_timeout_ms": 3000,
                "frame": write_47387.hex(),
            },
            struct.pack("<HBBHHB", 0x0020, 0x6B, 0x03, 5000, 3000, len(write_47387)) + write_47387,
            [],
            msg_id=43,
        ),
        described("CANCEL request 42", CANCEL, {}, b"", [], msg_id=42),
        described(
            "WELCOME from the Python gateway",
            WELCOME,
            {"ver_major": 0, "ver_minor": 1},
            struct.pack("<BB", 0, 1),
            [
                (0x0100, struct.pack("<I", 0xDEADBEEF)),
                (0x0105, struct.pack("<I", 0b111111)),
                (0x0102, b"thermaestro-gateway"),
                (0x0103, b"0.0.0"),
                (0x0106, struct.pack("<B", 3)),
                (0x0107, struct.pack("<B", 4)),
                (0x0108, struct.pack("<H", 5000)),
                (0x0109, struct.pack("<I", 20000)),
                (0x010A, struct.pack("<HH", 9999, 10000)),
                (0x010B, struct.pack("<H", 0x0020)),
                (0x010B, struct.pack("<H", 0x0019)),
                (0x0003, struct.pack("<I", 0b101)),
                (0x0004, struct.pack("<H", 120)),
                (0x0005, struct.pack("<H", 10)),
            ],
            msg_id=1,
            gw_time_us=123_456_789,
        ),
        described(
            "FATE: queued, one request ahead",
            FATE,
            {"stage": 1, "detail": 1, "stage_time_us": 1_000_000},
            struct.pack("<BBHQ", 1, 0, 1, 1_000_000),
            [],
            msg_id=42,
            gw_time_us=1_000_000,
        ),
        described(
            "FATE: dropped, queue full",
            FATE,
            {"stage": 6, "detail": 4, "stage_time_us": 1_000_500},
            struct.pack("<BBHQ", 6, 0, 4, 1_000_500),
            [],
            msg_id=44,
            gw_time_us=1_000_500,
        ),
        described(
            "FATE: pump ACKed the request",
            FATE,
            {"stage": 3, "detail": 0, "stage_time_us": 2_000_000},
            struct.pack("<BBHQ", 3, 0, 0, 2_000_000),
            [],
            msg_id=42,
            gw_time_us=2_000_000,
        ),
        described(
            "ANSWER: OK with the pump's 0x6A telegram",
            ANSWER,
            {"status": 1, "frame": answer.hex()},
            struct.pack("<BB", 1, len(answer)) + answer,
            [],
            msg_id=42,
            gw_time_us=3_000_000,
        ),
        described(
            "ANSWER: timeout",
            ANSWER,
            {"status": 2, "frame": ""},
            struct.pack("<BB", 2, 0),
            [],
            msg_id=45,
            gw_time_us=8_000_000,
        ),
        described(
            "FRAME: our read token answered with this client's request",
            FRAME,
            {
                "kind": 1,
                "origin": 3,
                "request_id": 42,
                "t_complete_us": 2_000_000,
                "t_reply_us": 1_995_000,
                "data": exchange.hex(),
            },
            struct.pack("<BBIQQH", 1, 3, 42, 2_000_000, 1_995_000, len(exchange)) + exchange,
            [],
            msg_id=17,
            gw_time_us=2_000_100,
        ),
        described(
            "HEALTH with counters and repeatable structured values",
            HEALTH,
            {},
            b"",
            [
                (0x0100, struct.pack("<I", 0xDEADBEEF)),
                (0x0104, struct.pack("<I", 3600)),
                (0x0301, struct.pack("<B", 1)),
                (0x0304, struct.pack("<I", 1000)),
                (0x030C, struct.pack("<HI", 4, 2)),
                (0x030C, struct.pack("<HI", 5, 1)),
                (0x0310, struct.pack("<I", 12)),
                (0x0315, struct.pack("<HBB", 0x0020, 0x69, 1)),
                (0x0318, struct.pack("<b", -61)),
            ],
            msg_id=3,
            gw_time_us=3_600_000_000,
        ),
        described(
            "ERROR: unsupported option",
            ERROR,
            {"code": 5},
            struct.pack("<H", 5),
            [(0x0200, struct.pack("<H", 0x8801)), (0x0201, b"unknown critical option")],
            msg_id=5,
            gw_time_us=10,
        ),
    ]

    hello = message(HELLO, struct.pack("<BB", 0, 0), [(0x0004, struct.pack("<H", 120))], 1)
    invalid = [
        {"name": "bad magic", "hex": "5458" + hello[2:].hex(), "error": "bad_magic"},
        {"name": "header cut short", "hex": hello[:12].hex(), "error": "malformed"},
        {
            "name": "body_len longer than the datagram",
            "hex": header(HELLO, b"\x00" * 9, 1).hex() + hello[20:].hex(),
            "error": "malformed",
        },
        {
            "name": "unknown message type",
            "hex": header(0x55, b"", 1).hex(),
            "error": "unknown_type",
        },
        {
            "name": "another major version",
            "hex": header(HELLO, hello[20:], 1, ver_major=7).hex() + hello[20:].hex(),
            "error": "bad_version",
        },
        {
            "name": "HELLO core cut short",
            "hex": message(HELLO, b"\x00", [], 1).hex(),
            "error": "malformed",
        },
        {
            "name": "option length runs past the body",
            "hex": message(HELLO, struct.pack("<BB", 0, 0) + struct.pack("<HH", 4, 9), [], 1).hex(),
            "error": "malformed",
        },
        {
            "name": "non-repeatable option twice",
            "hex": message(
                HELLO,
                struct.pack("<BB", 0, 0),
                [(0x0004, struct.pack("<H", 120)), (0x0004, struct.pack("<H", 60))],
                1,
            ).hex(),
            "error": "malformed",
        },
        {
            "name": "known option with the wrong length",
            "hex": message(
                HELLO, struct.pack("<BB", 0, 0), [(0x0004, struct.pack("<I", 120))], 1
            ).hex(),
            "error": "malformed",
        },
        {
            "name": "unknown critical option",
            "hex": message(HELLO, struct.pack("<BB", 0, 0), [(0x0042 | CRITICAL, b"")], 1).hex(),
            "error": "unsupported_option",
            "tag": 0x0042 | CRITICAL,
        },
        {
            "name": "client name longer than 32 bytes",
            "hex": message(HELLO, struct.pack("<BB", 0, 0), [(0x0001, b"x" * 33)], 1).hex(),
            "error": "malformed",
        },
        {
            "name": "client name not UTF-8",
            "hex": message(HELLO, struct.pack("<BB", 0, 0), [(0x0001, b"\xff\xfe")], 1).hex(),
            "error": "malformed",
        },
        {
            "name": "REQUEST frame_len runs past the body",
            "hex": message(
                REQUEST, struct.pack("<HBBHHB", 0x0020, 0x69, 2, 0, 0, 9) + read_47134, [], 1
            ).hex(),
            "error": "malformed",
        },
        {
            "name": "authenticated flag without a trailer",
            "hex": header(HELLO, hello[20:], 1, flags=0x80).hex() + hello[20:].hex(),
            "error": "malformed",
        },
    ]

    psk = bytes(range(32))
    nonce_g = bytes(range(0x20, 0x30))
    boot_id = 0xDEADBEEF
    session_key = hmac.new(
        psk,
        b"thermaestro-gw session" + nonce_c + nonce_g + struct.pack("<I", boot_id),
        hashlib.sha256,
    ).digest()

    def signed(
        msg_type: int,
        core: bytes,
        options: list[tuple[int, bytes]],
        msg_id: int,
        key: bytes,
        seq: int,
        gw_time_us: int = 0,
    ) -> str:
        body = core + b"".join(opt(t, v) for t, v in options)
        head = header(msg_type, body, msg_id, gw_time_us, flags=0x80)
        seq_bytes = struct.pack("<I", seq)
        mac = hmac.new(key, head + body + seq_bytes, hashlib.sha256).digest()[:16]
        return (head + body + seq_bytes + mac).hex()

    signed_hello = signed(
        HELLO, struct.pack("<BB", 0, 0), [(0x0002 | CRITICAL, nonce_c)], 1, psk, 1
    )
    signed_welcome = signed(
        WELCOME,
        struct.pack("<BB", 0, 1),
        [
            (0x0100, struct.pack("<I", boot_id)),
            (0x0105, struct.pack("<I", 0b1111111)),
            (0x0101, nonce_g),
        ],
        1,
        session_key,
        1,
        gw_time_us=500,
    )
    bad_mac = bytearray(bytes.fromhex(signed_hello))
    bad_mac[-1] ^= 0x01
    auth = {
        "psk": psk.hex(),
        "client_nonce": nonce_c.hex(),
        "gateway_nonce": nonce_g.hex(),
        "boot_id": boot_id,
        "session_key": session_key.hex(),
        "signed": [
            {"name": "HELLO signed with the PSK", "key": "psk", "seq": 1, "hex": signed_hello},
            {
                "name": "WELCOME signed with the session key",
                "key": "session_key",
                "seq": 1,
                "hex": signed_welcome,
            },
        ],
        "bad": [
            {
                "name": "HELLO with one bit of the MAC flipped",
                "key": "psk",
                "hex": bytes(bad_mac).hex(),
                "error": "auth_failed",
            },
        ],
    }

    return {
        "note": (
            "The Thermaestro gateway protocol, registry revision 1; see "
            "docs/gateway-protocol.md §3 to §7 and §11. Hex strings are whole "
            "datagrams. Options keep their full tag, critical bit included."
        ),
        "messages": messages,
        "invalid": invalid,
        "auth": auth,
    }


def render(data: dict[str, Any]) -> str:
    return json.dumps(data, indent=2) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="fail if the files are stale")
    args = parser.parse_args()
    files = {
        OUT / "nibe-frames.json": render(nibe_vectors()),
        OUT / "thermaestro-gw.json": render(gw_vectors()),
    }
    stale = [p for p, text in files.items() if not p.exists() or p.read_text() != text]
    if args.check:
        for p in stale:
            sys.stderr.write(f"{p.name} is out of date; run scripts/make-testvectors.py\n")
        return 1 if stale else 0
    OUT.mkdir(exist_ok=True)
    for p, text in files.items():
        p.write_text(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
