"""The Thermaestro gateway protocol (docs/gateway-protocol.md): the datagram codec.

One message per UDP datagram: a 20-byte header, a fixed core per message type, then TLV
options. An authenticated message ends with a trailer: a sequence number and a MAC.
Numbers are little-endian. The registry is the spec's §7, revision 2.
"""

import hashlib
import hmac
import struct
from dataclasses import dataclass
from enum import IntEnum, IntFlag
from typing import ClassVar

MAGIC = b"TG"
VERSION_MAJOR = 0
REGISTRY_REVISION = 2
MAX_DATAGRAM = 512
FLAG_AUTHENTICATED = 0x80
CRITICAL = 0x8000
MAC_LEN = 16

HEADER = struct.Struct("<2sBBBBHIQ")
TRAILER = struct.Struct(f"<I{MAC_LEN}s")
OPTION_HEAD = struct.Struct("<HH")
_SESSION_LABEL = b"thermaestro-gw session"


class MessageType(IntEnum):
    HELLO = 0x01
    KEEPALIVE = 0x02
    BYE = 0x03
    SUBSCRIBE = 0x04
    REQUEST = 0x10
    CANCEL = 0x11
    WELCOME = 0x81
    FATE = 0x90
    ANSWER = 0x91
    FRAME = 0xA0
    HEALTH = 0xB0
    ERROR = 0xE0


class Stage(IntEnum):
    QUEUED = 1
    SENT = 2
    PUMP_ACK = 3
    PUMP_NAK = 4
    NO_ACK_SEEN = 5
    DROPPED = 6


class DropReason(IntEnum):
    INVALID_FRAME = 1
    TOKEN_MISMATCH = 2
    UNKNOWN_KEY = 3
    QUEUE_FULL = 4
    EXPIRED = 5
    CANCELLED = 6
    EVICTED = 7
    UNSUPPORTED_OPTION = 8
    SHUTDOWN = 9


class AnswerStatus(IntEnum):
    OK = 1
    TIMEOUT = 2
    AMBIGUOUS = 3


class FrameKind(IntEnum):
    TO_GATEWAY = 1
    TO_OTHER = 2
    UNPARSED = 3


class Origin(IntEnum):
    NONE = 0
    ACK_ONLY = 1
    PLAIN_CLIENT = 2
    THIS_CLIENT = 3
    OTHER_CLIENT = 4
    CONSTANT = 5


class ErrorCode(IntEnum):
    BAD_MAGIC = 1
    BAD_VERSION = 2
    UNKNOWN_TYPE = 3
    MALFORMED = 4
    UNSUPPORTED_OPTION = 5
    NOT_SUBSCRIBED = 6
    TOO_MANY_CLIENTS = 7
    SOURCE_REFUSED = 8
    AUTH_REQUIRED = 9
    AUTH_FAILED = 10
    REPLAY = 11
    NO_SESSION = 12
    UNKNOWN_REQUEST = 13


class BusState(IntEnum):
    SILENT = 0
    ACTIVE = 1


class Subscription(IntFlag):
    FRAMES_ALL = 1 << 0
    FRAMES_OWN = 1 << 1
    HEALTH = 1 << 2


class Feature(IntFlag):
    FATE = 1 << 0
    ANSWER_PAIRING = 1 << 1
    FRAMES = 1 << 2
    HEALTH = 1 << 3
    PRIORITY = 1 << 4
    TTL = 1 << 5
    AUTH = 1 << 6


class RequestFlag(IntFlag):
    PRIORITY = 1 << 0
    EXPECT_ANSWER = 1 << 1


class Tag(IntEnum):
    CLIENT_NAME = 0x0001
    CLIENT_NONCE = 0x0002
    SUBSCRIBE = 0x0003
    LEASE_S = 0x0004
    HEALTH_INTERVAL_S = 0x0005
    BOOT_ID = 0x0100
    GATEWAY_NONCE = 0x0101
    IMPL = 0x0102
    IMPL_VERSION = 0x0103
    UPTIME_S = 0x0104
    FEATURES = 0x0105
    QUEUE_CAP = 0x0106
    MAX_CLIENTS = 0x0107
    ANSWER_TIMEOUT_MS = 0x0108
    TIMESTAMP_LAG_MAX_US = 0x0109
    PLAIN_PORTS = 0x010A
    ACK_ADDRESS = 0x010B
    PROTOCOL_SLOTS = 0x010C
    ERR_TAG = 0x0200
    ERR_DETAIL = 0x0201
    BUS_STATE = 0x0301
    MS_SINCE_LAST_BYTE = 0x0302
    MS_SINCE_LAST_TOKEN = 0x0303
    FRAMES_OK = 0x0304
    CRC_ERRORS = 0x0305
    NAKS_SENT = 0x0306
    INVALID_BYTES = 0x0307
    PUMP_NAKS = 0x0308
    NO_ACK_SEEN = 0x0309
    TOKENS_WITH_REPLY = 0x030A
    TOKENS_ACK_ONLY = 0x030B
    DROPS = 0x030C
    EVICTIONS = 0x030D
    AMBIGUOUS_ANSWERS = 0x030E
    ANSWER_TIMEOUTS = 0x030F
    LOOP_GAP_MAX_MS = 0x0310
    REPLY_PREP_MAX_US = 0x0311
    UDP_SEND_ERRORS = 0x0312
    EVENTS_DROPPED = 0x0313
    CLIENTS = 0x0314
    QUEUE_DEPTH = 0x0315
    AUTH_FAILURES = 0x0316
    REPLAYS_REJECTED = 0x0317
    WIFI_RSSI = 0x0318
    FREE_HEAP = 0x0319


@dataclass(frozen=True, slots=True)
class _Spec:
    fmt: str = ""
    """struct format of a fixed-size value; empty for text and opaque bytes."""
    size: int | None = None
    """Fixed size of an opaque value."""
    text: bool = False
    text_max: int | None = None
    repeatable: bool = False


_U8, _U16, _U32 = _Spec("B"), _Spec("H"), _Spec("I")
_SPECS: dict[int, _Spec] = {
    Tag.CLIENT_NAME: _Spec(text=True, text_max=32),
    Tag.CLIENT_NONCE: _Spec(size=16),
    Tag.SUBSCRIBE: _U32,
    Tag.LEASE_S: _U16,
    Tag.HEALTH_INTERVAL_S: _U16,
    Tag.BOOT_ID: _U32,
    Tag.GATEWAY_NONCE: _Spec(size=16),
    Tag.IMPL: _Spec(text=True),
    Tag.IMPL_VERSION: _Spec(text=True),
    Tag.UPTIME_S: _U32,
    Tag.FEATURES: _U32,
    Tag.QUEUE_CAP: _U8,
    Tag.PROTOCOL_SLOTS: _U8,
    Tag.MAX_CLIENTS: _U8,
    Tag.ANSWER_TIMEOUT_MS: _U16,
    Tag.TIMESTAMP_LAG_MAX_US: _U32,
    Tag.PLAIN_PORTS: _Spec("HH"),
    Tag.ACK_ADDRESS: _Spec("H", repeatable=True),
    Tag.ERR_TAG: _U16,
    Tag.ERR_DETAIL: _Spec(text=True),
    Tag.BUS_STATE: _U8,
    Tag.DROPS: _Spec("HI", repeatable=True),
    Tag.CLIENTS: _U8,
    Tag.QUEUE_DEPTH: _Spec("HBB", repeatable=True),
    Tag.WIFI_RSSI: _Spec("b"),
}
for _tag in Tag:
    _SPECS.setdefault(_tag, _U32)  # the remaining HEALTH counters


class ProtocolError(Exception):
    """A datagram this protocol refuses; `code` is what an ERROR reply would carry."""

    def __init__(
        self,
        code: ErrorCode,
        detail: str = "",
        *,
        tag: int | None = None,
        header: "Header | None" = None,
    ) -> None:
        super().__init__(f"{code.name.lower()}: {detail}" if detail else code.name.lower())
        self.code = code
        self.detail = detail
        self.tag = tag
        self.header = header


@dataclass(frozen=True, slots=True)
class Option:
    tag: int
    """The full tag, critical bit included."""
    value: bytes = b""

    @property
    def number(self) -> int:
        return self.tag & ~CRITICAL & 0xFFFF

    @property
    def critical(self) -> bool:
        return bool(self.tag & CRITICAL)

    @classmethod
    def pack(cls, tag: int, *values: int) -> "Option":
        """An option whose value is laid out as the registry says for `tag`."""
        return cls(tag, struct.pack("<" + _spec(tag).fmt, *values))

    @classmethod
    def u8(cls, tag: int, value: int) -> "Option":
        return cls(tag, struct.pack("<B", value))

    @classmethod
    def u16(cls, tag: int, value: int) -> "Option":
        return cls(tag, struct.pack("<H", value))

    @classmethod
    def u32(cls, tag: int, value: int) -> "Option":
        return cls(tag, struct.pack("<I", value))

    @classmethod
    def i8(cls, tag: int, value: int) -> "Option":
        return cls(tag, struct.pack("<b", value))

    @classmethod
    def text(cls, tag: int, value: str) -> "Option":
        return cls(tag, value.encode())

    def unpack(self) -> tuple[int, ...]:
        """The value's fields, as the registry lays them out."""
        values: tuple[int, ...] = struct.unpack("<" + _spec(self.tag).fmt, self.value)
        return values

    def as_int(self) -> int:
        (value,) = self.unpack()
        return value

    def as_text(self) -> str:
        return self.value.decode()


def _spec(tag: int) -> _Spec:
    spec = _SPECS.get(tag & ~CRITICAL & 0xFFFF)
    if spec is None or not spec.fmt:
        raise ValueError(f"option {tag:#06x} has no numeric layout in the registry")
    return spec


def find(options: tuple[Option, ...], tag: int) -> Option | None:
    """The option with this tag, critical or not."""
    return next((o for o in options if o.number == tag), None)


def find_all(options: tuple[Option, ...], tag: int) -> list[Option]:
    return [o for o in options if o.number == tag]


@dataclass(frozen=True, slots=True)
class Header:
    ver_major: int
    ver_minor: int
    type: int
    flags: int
    body_len: int
    id: int
    gw_time_us: int


# Messages. The fields before `id` are the message's fixed core, in wire order.


@dataclass(frozen=True, slots=True, kw_only=True)
class Hello:
    TYPE: ClassVar[MessageType] = MessageType.HELLO
    ver_min: int = VERSION_MAJOR
    ver_max: int = VERSION_MAJOR
    id: int = 0
    gw_time_us: int = 0
    options: tuple[Option, ...] = ()


@dataclass(frozen=True, slots=True, kw_only=True)
class Keepalive:
    TYPE: ClassVar[MessageType] = MessageType.KEEPALIVE
    id: int = 0
    gw_time_us: int = 0
    options: tuple[Option, ...] = ()


@dataclass(frozen=True, slots=True, kw_only=True)
class Bye:
    TYPE: ClassVar[MessageType] = MessageType.BYE
    id: int = 0
    gw_time_us: int = 0
    options: tuple[Option, ...] = ()


@dataclass(frozen=True, slots=True, kw_only=True)
class Subscribe:
    TYPE: ClassVar[MessageType] = MessageType.SUBSCRIBE
    id: int = 0
    gw_time_us: int = 0
    options: tuple[Option, ...] = ()


@dataclass(frozen=True, slots=True, kw_only=True)
class Request:
    TYPE: ClassVar[MessageType] = MessageType.REQUEST
    address: int
    token: int
    flags: int = 0
    ttl_ms: int = 0
    answer_timeout_ms: int = 0
    frame: bytes
    id: int = 0
    gw_time_us: int = 0
    options: tuple[Option, ...] = ()


@dataclass(frozen=True, slots=True, kw_only=True)
class Cancel:
    TYPE: ClassVar[MessageType] = MessageType.CANCEL
    id: int = 0
    gw_time_us: int = 0
    options: tuple[Option, ...] = ()


@dataclass(frozen=True, slots=True, kw_only=True)
class Welcome:
    TYPE: ClassVar[MessageType] = MessageType.WELCOME
    ver_major: int = VERSION_MAJOR
    ver_minor: int = REGISTRY_REVISION
    id: int = 0
    gw_time_us: int = 0
    options: tuple[Option, ...] = ()


@dataclass(frozen=True, slots=True, kw_only=True)
class Fate:
    TYPE: ClassVar[MessageType] = MessageType.FATE
    stage: int
    detail: int = 0
    stage_time_us: int
    id: int = 0
    gw_time_us: int = 0
    options: tuple[Option, ...] = ()


@dataclass(frozen=True, slots=True, kw_only=True)
class Answer:
    TYPE: ClassVar[MessageType] = MessageType.ANSWER
    status: int
    frame: bytes = b""
    id: int = 0
    gw_time_us: int = 0
    options: tuple[Option, ...] = ()


@dataclass(frozen=True, slots=True, kw_only=True)
class Frame:
    TYPE: ClassVar[MessageType] = MessageType.FRAME
    kind: int
    origin: int
    request_id: int = 0
    t_complete_us: int
    t_reply_us: int = 0
    data: bytes
    id: int = 0
    gw_time_us: int = 0
    options: tuple[Option, ...] = ()


@dataclass(frozen=True, slots=True, kw_only=True)
class Health:
    TYPE: ClassVar[MessageType] = MessageType.HEALTH
    id: int = 0
    gw_time_us: int = 0
    options: tuple[Option, ...] = ()


@dataclass(frozen=True, slots=True, kw_only=True)
class Error:
    TYPE: ClassVar[MessageType] = MessageType.ERROR
    code: int
    id: int = 0
    gw_time_us: int = 0
    options: tuple[Option, ...] = ()


Message = (
    Hello
    | Keepalive
    | Bye
    | Subscribe
    | Request
    | Cancel
    | Welcome
    | Fate
    | Answer
    | Frame
    | Health
    | Error
)

MESSAGE_CLASSES: dict[MessageType, type[Message]] = {
    cls.TYPE: cls
    for cls in (
        Hello,
        Keepalive,
        Bye,
        Subscribe,
        Request,
        Cancel,
        Welcome,
        Fate,
        Answer,
        Frame,
        Health,
        Error,
    )
}


@dataclass(frozen=True, slots=True)
class Decoded:
    header: Header
    message: Message
    seq: int | None
    """The trailer's sequence number on an authenticated message; check it with verify()."""


# --- encoding ------------------------------------------------------------------------------


def _with_length(data: bytes) -> bytes:
    return struct.pack("<B", len(data)) + data


def _core(msg: Message) -> bytes:
    match msg:
        case Hello():
            return struct.pack("<BB", msg.ver_min, msg.ver_max)
        case Welcome():
            return struct.pack("<BB", msg.ver_major, msg.ver_minor)
        case Request():
            head = struct.pack(
                "<HBBHH", msg.address, msg.token, msg.flags, msg.ttl_ms, msg.answer_timeout_ms
            )
            return head + _with_length(msg.frame)
        case Fate():
            return struct.pack("<BBHQ", msg.stage, 0, msg.detail, msg.stage_time_us)
        case Answer():
            return struct.pack("<B", msg.status) + _with_length(msg.frame)
        case Frame():
            head = struct.pack(
                "<BBIQQH",
                msg.kind,
                msg.origin,
                msg.request_id,
                msg.t_complete_us,
                msg.t_reply_us,
                len(msg.data),
            )
            return head + msg.data
        case Error():
            return struct.pack("<H", msg.code)
        case Keepalive() | Bye() | Subscribe() | Cancel() | Health():
            return b""


def encode(msg: Message, *, key: bytes | None = None, seq: int | None = None) -> bytes:
    """Encode a message; with `key` and `seq`, sign it (spec §11)."""
    body = _core(msg) + b"".join(
        OPTION_HEAD.pack(o.tag, len(o.value)) + o.value for o in msg.options
    )
    flags = FLAG_AUTHENTICATED if key is not None else 0
    size = HEADER.size + len(body) + (TRAILER.size if key is not None else 0)
    if size > MAX_DATAGRAM:
        raise ValueError(f"{msg.TYPE.name} would take {size} bytes, more than {MAX_DATAGRAM}")
    head = HEADER.pack(
        MAGIC,
        VERSION_MAJOR,
        REGISTRY_REVISION,
        msg.TYPE,
        flags,
        len(body),
        msg.id,
        msg.gw_time_us,
    )
    if key is None:
        return head + body
    if seq is None:
        raise ValueError("a signed message needs a sequence number")
    seq_bytes = struct.pack("<I", seq)
    return head + body + seq_bytes + _mac(key, head + body + seq_bytes)


# --- decoding ------------------------------------------------------------------------------


def decode(datagram: bytes) -> Decoded:
    """Decode one datagram, or raise ProtocolError with the code an ERROR reply would carry.

    A signed message's MAC isn't checked here, since which key applies depends on the
    session; call verify() for that.
    """
    if len(datagram) < HEADER.size:
        raise ProtocolError(ErrorCode.MALFORMED, f"{len(datagram)} bytes is shorter than a header")
    header = Header(*HEADER.unpack_from(datagram)[1:])
    if datagram[:2] != MAGIC:
        raise ProtocolError(ErrorCode.BAD_MAGIC, header=header)
    if header.ver_major != VERSION_MAJOR:
        raise ProtocolError(
            ErrorCode.BAD_VERSION, f"major version {header.ver_major}", header=header
        )
    try:
        msg_type = MessageType(header.type)
    except ValueError:
        raise ProtocolError(
            ErrorCode.UNKNOWN_TYPE, f"type {header.type:#04x}", header=header
        ) from None
    signed = bool(header.flags & FLAG_AUTHENTICATED)
    expected = HEADER.size + header.body_len + (TRAILER.size if signed else 0)
    if len(datagram) != expected:
        raise ProtocolError(
            ErrorCode.MALFORMED,
            f"{len(datagram)} bytes where the header implies {expected}",
            header=header,
        )
    body = datagram[HEADER.size : HEADER.size + header.body_len]
    try:
        message = _parse_body(msg_type, body, header.id, header.gw_time_us)
    except ProtocolError as e:
        e.header = header
        raise
    seq = TRAILER.unpack_from(datagram, expected - TRAILER.size)[0] if signed else None
    return Decoded(header=header, message=message, seq=seq)


class _Body:
    """Reads a message body front to back: the fixed core, then the options."""

    def __init__(self, msg_type: MessageType, data: bytes) -> None:
        self.msg_type = msg_type
        self.data = data
        self.at = 0

    def fixed(self, fmt: str) -> tuple[int, ...]:
        size = struct.calcsize("<" + fmt)
        if len(self.data) - self.at < size:
            raise ProtocolError(ErrorCode.MALFORMED, f"{self.msg_type.name} core cut short")
        values: tuple[int, ...] = struct.unpack_from("<" + fmt, self.data, self.at)
        self.at += size
        return values

    def counted(self, length: int, name: str) -> bytes:
        if len(self.data) - self.at < length:
            raise ProtocolError(
                ErrorCode.MALFORMED, f"{self.msg_type.name} {name} runs past the body"
            )
        value = bytes(self.data[self.at : self.at + length])
        self.at += length
        return value

    def options(self) -> tuple[Option, ...]:
        return _parse_options(self.data[self.at :])


def _parse_body(msg_type: MessageType, data: bytes, msg_id: int, gw_time_us: int) -> Message:
    b = _Body(msg_type, data)
    match msg_type:
        case MessageType.HELLO:
            ver_min, ver_max = b.fixed("BB")
            return Hello(
                ver_min=ver_min,
                ver_max=ver_max,
                id=msg_id,
                gw_time_us=gw_time_us,
                options=b.options(),
            )
        case MessageType.WELCOME:
            ver_major, ver_minor = b.fixed("BB")
            return Welcome(
                ver_major=ver_major,
                ver_minor=ver_minor,
                id=msg_id,
                gw_time_us=gw_time_us,
                options=b.options(),
            )
        case MessageType.REQUEST:
            address, token, flags, ttl_ms, answer_timeout_ms, frame_len = b.fixed("HBBHHB")
            return Request(
                address=address,
                token=token,
                flags=flags,
                ttl_ms=ttl_ms,
                answer_timeout_ms=answer_timeout_ms,
                frame=b.counted(frame_len, "frame"),
                id=msg_id,
                gw_time_us=gw_time_us,
                options=b.options(),
            )
        case MessageType.FATE:
            stage, _reserved, detail, stage_time_us = b.fixed("BBHQ")
            return Fate(
                stage=stage,
                detail=detail,
                stage_time_us=stage_time_us,
                id=msg_id,
                gw_time_us=gw_time_us,
                options=b.options(),
            )
        case MessageType.ANSWER:
            status, frame_len = b.fixed("BB")
            return Answer(
                status=status,
                frame=b.counted(frame_len, "frame"),
                id=msg_id,
                gw_time_us=gw_time_us,
                options=b.options(),
            )
        case MessageType.FRAME:
            kind, origin, request_id, t_complete_us, t_reply_us, data_len = b.fixed("BBIQQH")
            return Frame(
                kind=kind,
                origin=origin,
                request_id=request_id,
                t_complete_us=t_complete_us,
                t_reply_us=t_reply_us,
                data=b.counted(data_len, "data"),
                id=msg_id,
                gw_time_us=gw_time_us,
                options=b.options(),
            )
        case MessageType.ERROR:
            (code,) = b.fixed("H")
            return Error(code=code, id=msg_id, gw_time_us=gw_time_us, options=b.options())
        case MessageType.KEEPALIVE:
            return Keepalive(id=msg_id, gw_time_us=gw_time_us, options=b.options())
        case MessageType.BYE:
            return Bye(id=msg_id, gw_time_us=gw_time_us, options=b.options())
        case MessageType.SUBSCRIBE:
            return Subscribe(id=msg_id, gw_time_us=gw_time_us, options=b.options())
        case MessageType.CANCEL:
            return Cancel(id=msg_id, gw_time_us=gw_time_us, options=b.options())
        case MessageType.HEALTH:
            return Health(id=msg_id, gw_time_us=gw_time_us, options=b.options())


def _parse_options(data: bytes) -> tuple[Option, ...]:
    options: list[Option] = []
    seen: set[int] = set()
    at = 0
    while at < len(data):
        if len(data) - at < OPTION_HEAD.size:
            raise ProtocolError(ErrorCode.MALFORMED, "an option's head is cut short")
        tag, length = OPTION_HEAD.unpack_from(data, at)
        at += OPTION_HEAD.size
        if len(data) - at < length:
            raise ProtocolError(
                ErrorCode.MALFORMED, f"option {tag:#06x} runs past the body", tag=tag
            )
        option = Option(tag, bytes(data[at : at + length]))
        at += length
        spec = _SPECS.get(option.number)
        if spec is None:
            if option.critical:
                raise ProtocolError(
                    ErrorCode.UNSUPPORTED_OPTION, f"critical option {tag:#06x}", tag=tag
                )
        else:
            if option.number in seen and not spec.repeatable:
                raise ProtocolError(ErrorCode.MALFORMED, f"option {tag:#06x} twice", tag=tag)
            _check_value(option, spec)
        seen.add(option.number)
        options.append(option)
    return tuple(options)


def _check_value(option: Option, spec: _Spec) -> None:
    def refuse(why: str) -> ProtocolError:
        return ProtocolError(ErrorCode.MALFORMED, f"option {option.tag:#06x} {why}", tag=option.tag)

    if spec.fmt and len(option.value) != struct.calcsize("<" + spec.fmt):
        raise refuse(f"has {len(option.value)} bytes")
    if spec.size is not None and len(option.value) != spec.size:
        raise refuse(f"has {len(option.value)} bytes, not {spec.size}")
    if spec.text:
        try:
            option.value.decode()
        except UnicodeDecodeError:
            raise refuse("isn't UTF-8") from None
        if spec.text_max is not None and len(option.value) > spec.text_max:
            raise refuse(f"is longer than {spec.text_max} bytes")


# --- authentication (spec §11) --------------------------------------------------------------


def _mac(key: bytes, data: bytes) -> bytes:
    return hmac.new(key, data, hashlib.sha256).digest()[:MAC_LEN]


def session_key(psk: bytes, client_nonce: bytes, gateway_nonce: bytes, boot_id: int) -> bytes:
    message = _SESSION_LABEL + client_nonce + gateway_nonce + struct.pack("<I", boot_id)
    return hmac.new(psk, message, hashlib.sha256).digest()


def verify(datagram: bytes, key: bytes) -> int:
    """Check a signed datagram's MAC with `key` and return its sequence number.

    Call it after decode(), which has checked the datagram's structure.
    """
    if len(datagram) < HEADER.size + TRAILER.size or not datagram[5] & FLAG_AUTHENTICATED:
        raise ProtocolError(ErrorCode.AUTH_REQUIRED, "the datagram isn't signed")
    signed_part = datagram[:-MAC_LEN]
    seq, mac = TRAILER.unpack_from(datagram, len(datagram) - TRAILER.size)
    if not hmac.compare_digest(mac, _mac(key, signed_part)):
        raise ProtocolError(ErrorCode.AUTH_FAILED, "wrong MAC")
    sequence: int = seq
    return sequence
