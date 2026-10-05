"""The control port: sessions of the Thermaestro gateway protocol.

Specification: docs/gateway-protocol.md §11 and §12.

Pure logic with time passed in: a datagram comes in with its sender, and out come the
datagrams to send. Requests go to the engine; fates and answers come back from it; every
completed bus exchange becomes a FRAME for the sessions subscribed to it.

With a pre-shared key, authentication comes before anything else: a sender without a
session only ever hears `auth_required` or `auth_failed`, and every message of an
authenticated session is signed with its session key, in both directions.
"""

import os
from dataclasses import dataclass, field
from importlib.metadata import version

from thermaestro_gateway import nibe
from thermaestro_gateway import protocol as p
from thermaestro_gateway.bus import BusStats, Exchange
from thermaestro_gateway.engine import Engine, Outgoing, Queued

Address = tuple[str, int]
Datagram = tuple[Address, bytes]

IMPL = "thermaestro-gateway"
LEASE_S = (10, 120, 600)  # minimum, default, maximum
HEALTH_INTERVAL_S = (1, 10, 3600)
SILENT_AFTER_US = 5_000_000
ERROR_INTERVAL_US = 1_000_000
BASE_FEATURES = (
    p.Feature.FATE
    | p.Feature.ANSWER_PAIRING
    | p.Feature.FRAMES
    | p.Feature.HEALTH
    | p.Feature.PRIORITY
    | p.Feature.TTL
)


@dataclass(frozen=True, slots=True)
class Settings:
    psk: bytes | None = None
    boot_id: int = field(default_factory=lambda: int.from_bytes(os.urandom(4), "little"))
    plain_ports: tuple[int, int] | None = None
    """The plain NibeGW read and write ports, 0 where one is disabled; None if both are."""
    max_clients: int = 4
    sources: frozenset[str] | None = None
    """IP addresses allowed to use the control port; None allows any."""
    acknowledged: frozenset[int] = frozenset({nibe.MODBUS40})
    answer_timeout_ms: int = 5000
    timestamp_lag_max_us: int = 20_000
    started_us: int = 0


@dataclass(eq=False, slots=True)
class Session:
    addr: Address
    subscribe: p.Subscription
    lease_us: int
    health_interval_us: int
    expires_us: int
    next_health_us: int
    key: bytes | None = None
    recv_seq: int = 0
    send_seq: int = 0
    event_seq: int = 0
    loop_gap_max_ms: int = 0
    reply_prep_max_us: int = 0


@dataclass(slots=True)
class ControlStats:
    auth_failures: int = 0
    replays_rejected: int = 0
    udp_send_errors: int = 0
    events_dropped: int = 0


class Control:
    def __init__(self, engine: Engine, bus_stats: BusStats, settings: Settings) -> None:
        self.engine = engine
        self.bus_stats = bus_stats
        self.settings = settings
        self.stats = ControlStats()
        self._sessions: dict[Address, Session] = {}
        self._last_error_us: dict[Address, int] = {}
        self._bus_active = False

    @property
    def clients(self) -> int:
        return len(self._sessions)

    # --- datagrams in --------------------------------------------------------------------

    def handle(self, datagram: bytes, addr: Address, now_us: int) -> list[Datagram]:
        session = self._sessions.get(addr)
        try:
            decoded = p.decode(datagram)
        except p.ProtocolError as e:
            return self._refused(e, addr, session, now_us)
        msg = decoded.message
        if isinstance(msg, p.Hello):
            return self._hello(msg, decoded.seq is not None, datagram, addr, now_us)
        if session is None:
            code = p.ErrorCode.AUTH_REQUIRED if self.settings.psk else p.ErrorCode.NO_SESSION
            return self._error(addr, None, code, msg.id, now_us)
        if session.key is not None:
            refusal = self._check_signature(session, session.key, msg.id, datagram, now_us)
            if refusal is not None:
                return refusal
        session.expires_us = now_us + session.lease_us
        out: list[Datagram] = []
        match msg:
            case p.Keepalive():
                pass
            case p.Bye():
                self._end(session)
            case p.Subscribe():
                asked = p.find(msg.options, p.Tag.SUBSCRIBE)
                if asked is not None:
                    session.subscribe = _subscription(asked.as_int())
            case p.Request():
                self.engine.submit(session, msg, now_us)
            case p.Cancel():
                if not self.engine.cancel(session, msg.id, now_us):
                    out += self._error(addr, session, p.ErrorCode.UNKNOWN_REQUEST, msg.id, now_us)
            case _:
                out += self._error(addr, session, p.ErrorCode.UNKNOWN_TYPE, msg.id, now_us)
        return out + self._flush(now_us)

    def _refused(
        self, e: p.ProtocolError, addr: Address, session: Session | None, now_us: int
    ) -> list[Datagram]:
        if self.settings.psk and session is None:
            return self._error(addr, None, p.ErrorCode.AUTH_REQUIRED, 0, now_us)
        msg_id = e.header.id if e.header is not None else 0
        out = self._error(addr, session, e.code, msg_id, now_us, tag=e.tag, detail=e.detail)
        if (
            session is not None
            and e.code == p.ErrorCode.UNSUPPORTED_OPTION
            and e.header is not None
            and e.header.type == p.MessageType.REQUEST
        ):
            fate = p.Fate(
                id=msg_id,
                stage=p.Stage.DROPPED,
                detail=p.DropReason.UNSUPPORTED_OPTION,
                stage_time_us=now_us,
                gw_time_us=now_us,
            )
            out.append(self._send(session, fate))
        return out

    def _check_signature(
        self, session: Session, key: bytes, msg_id: int, datagram: bytes, now_us: int
    ) -> list[Datagram] | None:
        try:
            seq = p.verify(datagram, key)
        except p.ProtocolError:
            self.stats.auth_failures += 1
            return self._error(session.addr, session, p.ErrorCode.AUTH_FAILED, msg_id, now_us)
        if seq <= session.recv_seq:
            self.stats.replays_rejected += 1
            return self._error(session.addr, session, p.ErrorCode.REPLAY, msg_id, now_us)
        session.recv_seq = seq
        return None

    def _hello(
        self, msg: p.Hello, signed: bool, datagram: bytes, addr: Address, now_us: int
    ) -> list[Datagram]:
        s = self.settings
        if s.sources is not None and addr[0] not in s.sources:
            return self._error(addr, None, p.ErrorCode.SOURCE_REFUSED, msg.id, now_us)
        nonce = p.find(msg.options, p.Tag.CLIENT_NONCE)
        if s.psk is None:
            if nonce is not None and nonce.critical:
                return self._error(
                    addr, None, p.ErrorCode.UNSUPPORTED_OPTION, msg.id, now_us, tag=nonce.tag
                )
        else:
            if nonce is None or not signed:
                return self._error(addr, None, p.ErrorCode.AUTH_REQUIRED, msg.id, now_us)
            try:
                p.verify(datagram, s.psk)
            except p.ProtocolError:
                self.stats.auth_failures += 1
                return self._error(addr, None, p.ErrorCode.AUTH_FAILED, msg.id, now_us)
        if not msg.ver_min <= p.VERSION_MAJOR <= msg.ver_max:
            return self._error(addr, None, p.ErrorCode.BAD_VERSION, msg.id, now_us)
        old = self._sessions.get(addr)
        if old is None and len(self._sessions) >= s.max_clients:
            return self._error(addr, None, p.ErrorCode.TOO_MANY_CLIENTS, msg.id, now_us)
        if old is not None:
            self._end(old)

        def asked(tag: p.Tag, limits: tuple[int, int, int]) -> int:
            option = p.find(msg.options, tag)
            low, default, high = limits
            return default if option is None else min(max(option.as_int(), low), high)

        subscribe = p.find(msg.options, p.Tag.SUBSCRIBE)
        lease_s = asked(p.Tag.LEASE_S, LEASE_S)
        health_s = asked(p.Tag.HEALTH_INTERVAL_S, HEALTH_INTERVAL_S)
        session = Session(
            addr=addr,
            subscribe=_subscription(subscribe.as_int()) if subscribe else p.Subscription(0),
            lease_us=lease_s * 1_000_000,
            health_interval_us=health_s * 1_000_000,
            expires_us=now_us + lease_s * 1_000_000,
            next_health_us=now_us + health_s * 1_000_000,
        )
        options = [
            p.Option.u32(p.Tag.BOOT_ID, s.boot_id),
            p.Option.u32(p.Tag.FEATURES, BASE_FEATURES | (p.Feature.AUTH if s.psk else 0)),
        ]
        if s.psk is not None and nonce is not None:
            gateway_nonce = os.urandom(16)
            options.append(p.Option(p.Tag.GATEWAY_NONCE, gateway_nonce))
            session.key = p.session_key(s.psk, nonce.value, gateway_nonce, s.boot_id)
        options += [
            p.Option.text(p.Tag.IMPL, IMPL),
            p.Option.text(p.Tag.IMPL_VERSION, version("thermaestro-gateway")),
            p.Option.u32(p.Tag.UPTIME_S, self._uptime_s(now_us)),
            p.Option.u8(p.Tag.QUEUE_CAP, self.engine.queue_cap),
            p.Option.u8(p.Tag.MAX_CLIENTS, s.max_clients),
            p.Option.u16(p.Tag.ANSWER_TIMEOUT_MS, s.answer_timeout_ms),
            p.Option.u32(p.Tag.TIMESTAMP_LAG_MAX_US, s.timestamp_lag_max_us),
        ]
        if s.plain_ports is not None:
            options.append(p.Option.pack(p.Tag.PLAIN_PORTS, *s.plain_ports))
        options += [p.Option.u16(p.Tag.ACK_ADDRESS, a) for a in sorted(s.acknowledged)]
        options += [
            p.Option.u32(p.Tag.SUBSCRIBE, session.subscribe),
            p.Option.u16(p.Tag.LEASE_S, lease_s),
            p.Option.u16(p.Tag.HEALTH_INTERVAL_S, health_s),
        ]
        self._sessions[addr] = session
        welcome = p.Welcome(id=msg.id, gw_time_us=now_us, options=tuple(options))
        return [self._send(session, welcome)]

    # --- events out ----------------------------------------------------------------------

    def on_exchange(self, exchange: Exchange) -> list[Datagram]:
        self.engine.on_exchange(exchange)
        now = exchange.t_complete_us
        owner = self.engine.owner_of(exchange.reply) if exchange.reply is not None else None
        out: list[Datagram] = []
        for session in self._sessions.values():
            own = owner is not None and owner[0] is session
            if not (
                p.Subscription.FRAMES_ALL in session.subscribe
                or (own and p.Subscription.FRAMES_OWN in session.subscribe)
            ):
                continue
            session.event_seq += 1
            frame = p.Frame(
                id=session.event_seq,
                gw_time_us=now,
                kind=exchange.kind,
                origin=self._origin(exchange, session),
                request_id=owner[1] if own and owner is not None else 0,
                t_complete_us=exchange.t_complete_us,
                t_reply_us=exchange.t_reply_us or 0,
                data=exchange.data,
            )
            out.append(self._send(session, frame))
        return out + self._flush(now)

    def tick(self, now_us: int) -> list[Datagram]:
        self.engine.tick(now_us)
        for session in [s for s in self._sessions.values() if s.expires_us <= now_us]:
            self._end(session)
        last = self.bus_stats.last_byte_us
        active = last is not None and now_us - last < SILENT_AFTER_US
        changed, self._bus_active = active != self._bus_active, active
        out: list[Datagram] = []
        for session in self._sessions.values():
            if p.Subscription.HEALTH not in session.subscribe:
                continue
            if changed or now_us >= session.next_health_us:
                session.next_health_us = now_us + session.health_interval_us
                out.append(self._send(session, self._health(session, now_us)))
        self._last_error_us = {
            a: t for a, t in self._last_error_us.items() if now_us - t < ERROR_INTERVAL_US
        }
        return out + self._flush(now_us)

    def shutdown(self, now_us: int) -> list[Datagram]:
        """The gateway is stopping: report the queued requests as dropped."""
        self.engine.shutdown(now_us)
        return self._flush(now_us)

    def note_loop_gap(self, gap_ms: int) -> None:
        for session in self._sessions.values():
            session.loop_gap_max_ms = max(session.loop_gap_max_ms, gap_ms)

    def note_reply_prep(self, prep_us: int) -> None:
        for session in self._sessions.values():
            session.reply_prep_max_us = max(session.reply_prep_max_us, prep_us)

    # --- helpers -------------------------------------------------------------------------

    def _origin(self, exchange: Exchange, session: Session) -> p.Origin:
        if exchange.reply is None:
            return p.Origin.ACK_ONLY if exchange.kind == p.FrameKind.TO_GATEWAY else p.Origin.NONE
        ref = exchange.reply.ref
        if not isinstance(ref, Queued):
            return p.Origin.CONSTANT
        if ref.client is None:
            return p.Origin.PLAIN_CLIENT
        return p.Origin.THIS_CLIENT if ref.client is session else p.Origin.OTHER_CLIENT

    def _health(self, session: Session, now_us: int) -> p.Health:
        b, e, c = self.bus_stats, self.engine.stats, self.stats
        u32 = p.Option.u32
        options = [
            u32(p.Tag.BOOT_ID, self.settings.boot_id),
            u32(p.Tag.UPTIME_S, self._uptime_s(now_us)),
            p.Option.u8(
                p.Tag.BUS_STATE, p.BusState.ACTIVE if self._bus_active else p.BusState.SILENT
            ),
        ]
        if b.last_byte_us is not None:
            options.append(u32(p.Tag.MS_SINCE_LAST_BYTE, _ms_since(b.last_byte_us, now_us)))
        if b.last_token_us is not None:
            options.append(u32(p.Tag.MS_SINCE_LAST_TOKEN, _ms_since(b.last_token_us, now_us)))
        options += [
            u32(p.Tag.FRAMES_OK, b.frames_ok),
            u32(p.Tag.CRC_ERRORS, b.crc_errors),
            u32(p.Tag.NAKS_SENT, b.naks_sent),
            u32(p.Tag.INVALID_BYTES, b.invalid_bytes),
            u32(p.Tag.PUMP_NAKS, b.pump_naks),
            u32(p.Tag.NO_ACK_SEEN, b.no_ack_seen),
            u32(p.Tag.TOKENS_WITH_REPLY, b.tokens_with_reply),
            u32(p.Tag.TOKENS_ACK_ONLY, b.tokens_ack_only),
        ]
        options += [p.Option.pack(p.Tag.DROPS, r, n) for r, n in sorted(e.drops.items())]
        options += [
            u32(p.Tag.EVICTIONS, e.evictions),
            u32(p.Tag.AMBIGUOUS_ANSWERS, e.ambiguous_answers),
            u32(p.Tag.ANSWER_TIMEOUTS, e.answer_timeouts),
            u32(p.Tag.LOOP_GAP_MAX_MS, session.loop_gap_max_ms),
            u32(p.Tag.REPLY_PREP_MAX_US, session.reply_prep_max_us),
            u32(p.Tag.UDP_SEND_ERRORS, c.udp_send_errors),
            u32(p.Tag.EVENTS_DROPPED, c.events_dropped),
            p.Option.u8(p.Tag.CLIENTS, len(self._sessions)),
        ]
        options += [
            p.Option.pack(p.Tag.QUEUE_DEPTH, address, token, depth)
            for (address, token), depth in sorted(self.engine.depths().items())
        ]
        if self.settings.psk is not None:
            options += [
                u32(p.Tag.AUTH_FAILURES, c.auth_failures),
                u32(p.Tag.REPLAYS_REJECTED, c.replays_rejected),
            ]
        session.loop_gap_max_ms = 0
        session.reply_prep_max_us = 0
        session.event_seq += 1
        return p.Health(id=session.event_seq, gw_time_us=now_us, options=tuple(options))

    def _flush(self, now_us: int) -> list[Datagram]:
        out: list[Datagram] = []
        for item in self.engine.take_outbox():
            session = self._session_of(item)
            if session is not None:
                out.append(self._send(session, item.message))
        return out

    def _session_of(self, item: Outgoing) -> Session | None:
        client = item.client
        if isinstance(client, Session) and self._sessions.get(client.addr) is client:
            return client
        return None

    def _send(self, session: Session, msg: p.Message) -> Datagram:
        if session.key is None:
            return session.addr, p.encode(msg)
        session.send_seq += 1
        return session.addr, p.encode(msg, key=session.key, seq=session.send_seq)

    def _error(
        self,
        addr: Address,
        session: Session | None,
        code: p.ErrorCode,
        msg_id: int,
        now_us: int,
        *,
        tag: int | None = None,
        detail: str = "",
    ) -> list[Datagram]:
        last = self._last_error_us.get(addr)
        if last is not None and now_us - last < ERROR_INTERVAL_US:
            return []
        self._last_error_us[addr] = now_us
        options: list[p.Option] = []
        if tag is not None:
            options.append(p.Option.u16(p.Tag.ERR_TAG, tag))
        if detail:
            options.append(p.Option.text(p.Tag.ERR_DETAIL, detail[:200]))
        error = p.Error(id=msg_id, gw_time_us=now_us, code=code, options=tuple(options))
        if session is not None:
            return [self._send(session, error)]
        return [(addr, p.encode(error))]

    def _end(self, session: Session) -> None:
        if self._sessions.get(session.addr) is session:
            del self._sessions[session.addr]
        self.engine.forget(session)

    def _uptime_s(self, now_us: int) -> int:
        return (now_us - self.settings.started_us) // 1_000_000


def _ms_since(then_us: int, now_us: int) -> int:
    return max(0, now_us - then_us) // 1000


def _subscription(mask: int) -> p.Subscription:
    return p.Subscription(
        mask & (p.Subscription.FRAMES_ALL | p.Subscription.FRAMES_OWN | p.Subscription.HEALTH)
    )
