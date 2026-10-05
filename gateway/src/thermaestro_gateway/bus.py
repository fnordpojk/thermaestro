"""The gateway's side of the MODBUS40 bus, as a pure state machine.

Bytes from the pump go in one at a time with a timestamp; out come the bytes to write
back at once and, when an exchange completes, the exchange. No I/O happens here, so a
simulated pump can drive every path in a test.

The bus (docs/gateway-protocol.md §2): the pump sends telegrams to addresses. For an address this
gateway acknowledges, a token (a telegram without data) gets a reply or an ACK, and a
telegram with data gets an ACK, or a NAK when its checksum is wrong; after a reply, the
pump sends ACK or NAK. Telegrams to other addresses are followed, not answered: that
device's reply and the pump's ACK/NAK belong to the same exchange. Written from that
description; nothing is taken from the EPL-licensed NibeGW state machine.
"""

from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Protocol

from thermaestro_gateway import nibe
from thermaestro_gateway.protocol import FrameKind


@dataclass(frozen=True, slots=True)
class Reply:
    frame: bytes
    ref: object = None
    """Whatever the responder wants back with the exchange, e.g. the queued request."""


class Responder(Protocol):
    def reply_for(self, address: int, command: int, now_us: int) -> Reply | None:
        """The reply for a token the pump just sent, or None to ACK it."""


@dataclass(frozen=True, slots=True)
class Exchange:
    data: bytes
    """Every byte of the exchange as it was on the bus; what a NibeGW datagram carries."""
    kind: FrameKind
    telegram: nibe.Telegram | None
    """The pump's telegram, or None when its checksum was wrong."""
    reply: Reply | None
    """The reply this gateway sent."""
    trailer: int | None
    """The single byte that closed the exchange: the pump's ACK/NAK after a reply, the
    ACK/NAK this gateway sent, or another device's; None if the next telegram began."""
    t_complete_us: int
    t_reply_us: int | None = None
    refused: Reply | None = None
    """A reply the responder offered that wasn't valid for this token, so an ACK went out."""


@dataclass(slots=True)
class BusStats:
    frames_ok: int = 0
    crc_errors: int = 0
    naks_sent: int = 0
    invalid_bytes: int = 0
    pump_naks: int = 0
    no_ack_seen: int = 0
    tokens_with_reply: int = 0
    tokens_ack_only: int = 0
    last_byte_us: int | None = None
    last_token_us: int | None = None
    """The last token to an address this gateway acknowledges."""


@dataclass(frozen=True, slots=True)
class Step:
    write: bytes = b""
    done: Exchange | None = None


class _State(Enum):
    IDLE = auto()
    TELEGRAM = auto()
    AFTER_OUR_REPLY = auto()
    OTHER_REPLY_START = auto()
    OTHER_REPLY = auto()
    AFTER_OTHER_REPLY = auto()


@dataclass(slots=True)
class _Open:
    """The exchange being collected."""

    data: bytearray = field(default_factory=bytearray)
    telegram: nibe.Telegram | None = None
    reply: Reply | None = None
    t_reply_us: int | None = None
    other_reply_at: int = 0


class Bus:
    def __init__(self, acknowledged: frozenset[int], responder: Responder) -> None:
        self.acknowledged = acknowledged
        self.responder = responder
        self.stats = BusStats()
        self._state = _State.IDLE
        self._after_start = False
        self._open = _Open()

    def feed(self, byte: int, now_us: int) -> Step:
        self.stats.last_byte_us = now_us
        match self._state:
            case _State.IDLE:
                return self._idle(byte)
            case _State.TELEGRAM:
                return self._telegram_byte(byte, now_us)
            case _State.AFTER_OUR_REPLY | _State.AFTER_OTHER_REPLY:
                return self._closing_byte(byte, now_us)
            case _State.OTHER_REPLY_START:
                return self._other_reply_start(byte, now_us)
            case _State.OTHER_REPLY:
                return self._other_reply_byte(byte)

    def _idle(self, byte: int) -> Step:
        # A telegram starts with 0x5C followed by anything but another 0x5C;
        # a doubled start byte is ignored.
        if byte == nibe.START_TELEGRAM:
            self._after_start = not self._after_start
        elif self._after_start:
            self._after_start = False
            self._open = _Open(data=bytearray((nibe.START_TELEGRAM, byte)))
            self._state = _State.TELEGRAM
        else:
            self.stats.invalid_bytes += 1
        return Step()

    def _telegram_byte(self, byte: int, now_us: int) -> Step:
        data = self._open.data
        data.append(byte)
        if len(data) != nibe.telegram_length(bytes(data)):
            return Step()
        address = int.from_bytes(data[1:3], "big")
        ours = address in self.acknowledged
        try:
            telegram = nibe.parse_telegram(bytes(data))
        except nibe.FrameError:
            self.stats.crc_errors += 1
            if ours:
                self.stats.naks_sent += 1
                return self._finish(FrameKind.UNPARSED, now_us, nibe.NAK, write=True)
            return self._finish(FrameKind.UNPARSED, now_us, None)
        self.stats.frames_ok += 1
        self._open.telegram = telegram
        if not ours:
            self._state = _State.OTHER_REPLY_START
            return Step()
        if not telegram.is_token:
            return self._finish(FrameKind.TO_GATEWAY, now_us, nibe.ACK, write=True)
        self.stats.last_token_us = now_us
        offered = self.responder.reply_for(address, telegram.command, now_us)
        if offered is not None and _fits(offered.frame, telegram.command):
            self.stats.tokens_with_reply += 1
            self._open.reply = offered
            self._open.t_reply_us = now_us
            self._open.data += offered.frame
            self._state = _State.AFTER_OUR_REPLY
            return Step(write=offered.frame)
        self.stats.tokens_ack_only += 1
        return self._finish(FrameKind.TO_GATEWAY, now_us, nibe.ACK, write=True, refused=offered)

    def _other_reply_start(self, byte: int, now_us: int) -> Step:
        if byte == nibe.START_REPLY:
            self._open.other_reply_at = len(self._open.data)
            self._open.data.append(byte)
            self._state = _State.OTHER_REPLY
            return Step()
        return self._closing_byte(byte, now_us)

    def _other_reply_byte(self, byte: int) -> Step:
        data = self._open.data
        data.append(byte)
        reply = data[self._open.other_reply_at :]
        if len(reply) >= 3 and len(reply) == nibe.REPLY_OVERHEAD + reply[2]:
            self._state = _State.AFTER_OTHER_REPLY
        return Step()

    def _closing_byte(self, byte: int, now_us: int) -> Step:
        ours = self._state is _State.AFTER_OUR_REPLY
        kind = (
            FrameKind.TO_GATEWAY if self._open.telegram and self._is_ours() else FrameKind.TO_OTHER
        )
        if byte == nibe.START_TELEGRAM:
            # The next telegram began without an ACK/NAK; this byte starts it.
            if ours:
                self.stats.no_ack_seen += 1
            step = self._finish(kind, now_us, None)
            self._after_start = True
            return step
        if ours and byte == nibe.NAK:
            self.stats.pump_naks += 1
        self._open.data.append(byte)
        return self._finish(kind, now_us, byte, already_in_data=True)

    def _is_ours(self) -> bool:
        telegram = self._open.telegram
        return telegram is not None and telegram.address in self.acknowledged

    def _finish(
        self,
        kind: FrameKind,
        now_us: int,
        trailer: int | None,
        *,
        write: bool = False,
        already_in_data: bool = False,
        refused: Reply | None = None,
    ) -> Step:
        if trailer is not None and not already_in_data:
            self._open.data.append(trailer)
        o = self._open
        exchange = Exchange(
            data=bytes(o.data),
            kind=kind,
            telegram=o.telegram,
            reply=o.reply,
            trailer=trailer,
            t_complete_us=now_us,
            t_reply_us=o.t_reply_us,
            refused=refused,
        )
        self._state = _State.IDLE
        self._open = _Open()
        return Step(
            write=bytes((trailer,)) if write and trailer is not None else b"", done=exchange
        )


def _fits(frame: bytes, token: int) -> bool:
    try:
        nibe.validate_reply(frame)
    except nibe.FrameError:
        return False
    return frame[1] == token
