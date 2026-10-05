from thermaestro_gateway import nibe
from thermaestro_gateway import protocol as p
from thermaestro_gateway.bus import BusStats, Exchange, Reply
from thermaestro_gateway.control import Control, Settings
from thermaestro_gateway.engine import Engine, Queued

A = ("192.0.2.10", 40000)
B = ("192.0.2.11", 40001)
READ_KEY = (nibe.MODBUS40, nibe.READ_TOKEN)
WRITE_KEY = (nibe.MODBUS40, nibe.WRITE_TOKEN)
READ_TOKEN = bytes.fromhex("5c0020690049")
PSK = bytes(range(32))
NONCE = bytes(range(0x10, 0x20))


def make(psk: bytes | None = None, max_clients: int = 4) -> tuple[Control, Engine, BusStats]:
    engine = Engine(keys={READ_KEY, WRITE_KEY})
    stats = BusStats()
    settings = Settings(
        psk=psk, boot_id=0xDEADBEEF, plain_ports=(9999, 10000), max_clients=max_clients
    )
    return Control(engine, stats, settings), engine, stats


def send(
    control: Control,
    addr: tuple[str, int],
    msg: p.Message,
    now_us: int = 0,
    *,
    key: bytes | None = None,
    seq: int | None = None,
) -> list[p.Decoded]:
    datagram = p.encode(msg, key=key, seq=seq)
    return [p.decode(d) for to, d in control.handle(datagram, addr, now_us) if to == addr]


def only(replies: list[p.Decoded]) -> p.Message:
    assert len(replies) == 1, replies
    return replies[0].message


def hello(*options: p.Option, msg_id: int = 1) -> p.Hello:
    return p.Hello(id=msg_id, options=options)


def opt(msg: p.Message, tag: p.Tag) -> p.Option:
    found = p.find(msg.options, tag)
    assert found is not None, tag
    return found


def test_hello_gets_a_welcome() -> None:
    control, _, _ = make()
    welcome = only(send(control, A, hello(p.Option.u16(p.Tag.LEASE_S, 60), msg_id=5), 1000))
    assert isinstance(welcome, p.Welcome)
    assert (welcome.id, welcome.gw_time_us) == (5, 1000)
    assert opt(welcome, p.Tag.BOOT_ID).as_int() == 0xDEADBEEF
    assert opt(welcome, p.Tag.LEASE_S).as_int() == 60
    assert opt(welcome, p.Tag.IMPL).as_text() == "thermaestro-gw-python"
    assert opt(welcome, p.Tag.PLAIN_PORTS).unpack() == (9999, 10000)
    assert [o.as_int() for o in p.find_all(welcome.options, p.Tag.ACK_ADDRESS)] == [0x0020]
    features = p.Feature(opt(welcome, p.Tag.FEATURES).as_int())
    assert p.Feature.FATE in features
    assert p.Feature.AUTH not in features
    assert control.clients == 1


def test_lease_is_clamped() -> None:
    control, _, _ = make()
    welcome = only(send(control, A, hello(p.Option.u16(p.Tag.LEASE_S, 1))))
    assert opt(welcome, p.Tag.LEASE_S).as_int() == 10


def test_a_client_outside_the_version_range_is_refused() -> None:
    control, _, _ = make()
    error = only(send(control, A, p.Hello(id=3, ver_min=1, ver_max=2)))
    assert isinstance(error, p.Error)
    assert (error.code, error.id) == (p.ErrorCode.BAD_VERSION, 3)
    assert control.clients == 0


def test_messages_without_a_session_are_refused() -> None:
    control, _, _ = make()
    error = only(send(control, A, p.Keepalive(id=9)))
    assert isinstance(error, p.Error)
    assert error.code == p.ErrorCode.NO_SESSION


def test_too_many_clients() -> None:
    control, _, _ = make(max_clients=1)
    send(control, A, hello())
    error = only(send(control, B, hello()))
    assert isinstance(error, p.Error)
    assert error.code == p.ErrorCode.TOO_MANY_CLIENTS


def test_a_source_filter_refuses_other_addresses() -> None:
    engine = Engine(keys={READ_KEY})
    control = Control(engine, BusStats(), Settings(sources=frozenset({"192.0.2.99"})))
    error = only(send(control, A, hello()))
    assert isinstance(error, p.Error)
    assert error.code == p.ErrorCode.SOURCE_REFUSED


def test_a_request_is_queued_and_its_fate_reported() -> None:
    control, engine, _ = make()
    send(control, A, hello())
    request = p.Request(id=42, address=0x20, token=0x69, frame=nibe.read_request(47134))
    fate = only(send(control, A, request, 5000))
    assert isinstance(fate, p.Fate)
    assert (fate.id, fate.stage) == (42, p.Stage.QUEUED)
    assert engine.depths()[READ_KEY] == 1


def test_cancel_of_an_unknown_request() -> None:
    control, _, _ = make()
    send(control, A, hello())
    error = only(send(control, A, p.Cancel(id=77)))
    assert isinstance(error, p.Error)
    assert (error.code, error.id) == (p.ErrorCode.UNKNOWN_REQUEST, 77)


def test_a_request_with_an_unknown_critical_option() -> None:
    control, _, _ = make()
    send(control, A, hello())
    request = p.Request(
        id=8,
        address=0x20,
        token=0x69,
        frame=nibe.read_request(47134),
        options=(p.Option(0x0499 | p.CRITICAL, b""),),
    )
    replies = [r.message for r in send(control, A, request)]
    assert any(isinstance(m, p.Error) and m.code == p.ErrorCode.UNSUPPORTED_OPTION for m in replies)
    assert any(
        isinstance(m, p.Fate)
        and m.stage == p.Stage.DROPPED
        and m.detail == p.DropReason.UNSUPPORTED_OPTION
        for m in replies
    )


def exchange_for(engine: Engine, now_us: int) -> Exchange:
    reply = engine.reply_for(nibe.MODBUS40, nibe.READ_TOKEN, now_us)
    data = READ_TOKEN + (reply.frame if reply else b"") + b"\x06"
    return Exchange(
        data=data,
        kind=p.FrameKind.TO_GATEWAY,
        telegram=nibe.parse_telegram(READ_TOKEN),
        reply=reply,
        trailer=nibe.ACK,
        t_complete_us=now_us + 20_000,
        t_reply_us=now_us if reply else None,
    )


def test_frames_go_to_subscribers_with_the_origin_per_recipient() -> None:
    control, engine, _ = make()
    send(control, A, hello(p.Option.u32(p.Tag.SUBSCRIBE, p.Subscription.FRAMES_OWN)))
    send(control, B, hello(p.Option.u32(p.Tag.SUBSCRIBE, p.Subscription.FRAMES_ALL)))
    request = p.Request(id=42, address=0x20, token=0x69, frame=nibe.read_request(47134))
    send(control, A, request)
    out = control.on_exchange(exchange_for(engine, 1_000_000))
    frames = {
        to: p.decode(d).message for to, d in out if p.decode(d).header.type == p.MessageType.FRAME
    }
    a, b = frames[A], frames[B]
    assert isinstance(a, p.Frame)
    assert isinstance(b, p.Frame)
    assert (a.origin, a.request_id) == (p.Origin.THIS_CLIENT, 42)
    assert (b.origin, b.request_id) == (p.Origin.OTHER_CLIENT, 0)
    assert a.data == b.data
    # An ACK-only exchange is nobody's own, so only the FRAMES_ALL client sees it.
    out = control.on_exchange(exchange_for(engine, 2_000_000))
    assert {to for to, d in out if p.decode(d).header.type == p.MessageType.FRAME} == {B}


def test_plain_and_constant_origins() -> None:
    control, _, _ = make()
    send(control, A, hello(p.Option.u32(p.Tag.SUBSCRIBE, p.Subscription.FRAMES_ALL)))
    plain_request = Queued(frame=nibe.read_request(1), key=READ_KEY)
    for ref, origin in ((plain_request, p.Origin.PLAIN_CLIENT), (None, p.Origin.CONSTANT)):
        exchange = Exchange(
            data=READ_TOKEN + nibe.read_request(1) + b"\x06",
            kind=p.FrameKind.TO_GATEWAY,
            telegram=nibe.parse_telegram(READ_TOKEN),
            reply=Reply(nibe.read_request(1), ref=ref),
            trailer=nibe.ACK,
            t_complete_us=10,
            t_reply_us=5,
        )
        (out,) = control.on_exchange(exchange)
        frame = p.decode(out[1]).message
        assert isinstance(frame, p.Frame)
        assert frame.origin == origin


def test_the_lease_runs_out() -> None:
    control, engine, _ = make()
    send(control, A, hello(p.Option.u16(p.Tag.LEASE_S, 10)))
    send(control, A, p.Request(id=1, address=0x20, token=0x69, frame=nibe.read_request(1)))
    control.tick(9_000_000)
    assert control.clients == 1
    control.tick(10_000_001)
    assert control.clients == 0
    assert engine.depths()[READ_KEY] == 0


def test_keepalive_renews_and_bye_ends() -> None:
    control, _, _ = make()
    send(control, A, hello(p.Option.u16(p.Tag.LEASE_S, 10)))
    send(control, A, p.Keepalive(), 8_000_000)
    control.tick(15_000_000)
    assert control.clients == 1
    send(control, A, p.Bye(), 15_000_000)
    assert control.clients == 0


def test_health_on_its_interval() -> None:
    control, _, stats = make()
    stats.frames_ok = 1234
    send(
        control,
        A,
        hello(
            p.Option.u32(p.Tag.SUBSCRIBE, p.Subscription.HEALTH),
            p.Option.u16(p.Tag.HEALTH_INTERVAL_S, 5),
        ),
    )
    assert control.tick(4_000_000) == []
    ((to, datagram),) = control.tick(5_000_000)
    health = p.decode(datagram).message
    assert isinstance(health, p.Health)
    assert to == A
    assert health.id == 1
    assert opt(health, p.Tag.FRAMES_OK).as_int() == 1234
    assert opt(health, p.Tag.CLIENTS).as_int() == 1
    assert opt(health, p.Tag.UPTIME_S).as_int() == 5
    assert opt(health, p.Tag.BUS_STATE).as_int() == p.BusState.SILENT


def test_health_at_once_when_the_bus_wakes_up() -> None:
    control, _, stats = make()
    send(control, A, hello(p.Option.u32(p.Tag.SUBSCRIBE, p.Subscription.HEALTH)))
    assert control.tick(1_000_000) == []
    stats.last_byte_us = 1_500_000
    ((_, datagram),) = control.tick(1_600_000)
    health = p.decode(datagram).message
    assert opt(health, p.Tag.BUS_STATE).as_int() == p.BusState.ACTIVE
    assert opt(health, p.Tag.MS_SINCE_LAST_BYTE).as_int() == 100
    assert control.tick(1_700_000) == []


def test_errors_are_rate_limited() -> None:
    control, _, _ = make()
    assert len(send(control, A, p.Keepalive(), 0)) == 1
    assert send(control, A, p.Keepalive(), 100_000) == []
    assert len(send(control, A, p.Keepalive(), 1_100_000)) == 1


def test_critical_nonce_to_a_gateway_without_a_psk() -> None:
    control, _, _ = make()
    error = only(send(control, A, hello(p.Option(p.Tag.CLIENT_NONCE | p.CRITICAL, NONCE))))
    assert isinstance(error, p.Error)
    assert error.code == p.ErrorCode.UNSUPPORTED_OPTION
    assert control.clients == 0


# --- with a PSK --------------------------------------------------------------------------


def test_an_unsigned_hello_needs_authentication() -> None:
    control, _, _ = make(psk=PSK)
    error = only(send(control, A, hello()))
    assert isinstance(error, p.Error)
    assert error.code == p.ErrorCode.AUTH_REQUIRED
    assert control.clients == 0


def test_a_hello_signed_with_the_wrong_key() -> None:
    control, _, _ = make(psk=PSK)
    signed = hello(p.Option(p.Tag.CLIENT_NONCE | p.CRITICAL, NONCE))
    error = only(send(control, A, signed, key=b"x" * 32, seq=1))
    assert isinstance(error, p.Error)
    assert error.code == p.ErrorCode.AUTH_FAILED


def test_an_authenticated_session() -> None:
    control, engine, _ = make(psk=PSK)
    signed = hello(p.Option(p.Tag.CLIENT_NONCE | p.CRITICAL, NONCE))
    datagrams = control.handle(p.encode(signed, key=PSK, seq=1), A, 0)
    ((_, welcome_bytes),) = datagrams
    welcome = p.decode(welcome_bytes).message
    gateway_nonce = opt(welcome, p.Tag.GATEWAY_NONCE).value
    key = p.session_key(PSK, NONCE, gateway_nonce, 0xDEADBEEF)
    assert p.verify(welcome_bytes, key) == 1
    assert p.Feature.AUTH in p.Feature(opt(welcome, p.Tag.FEATURES).as_int())

    request = p.Request(id=5, address=0x20, token=0x69, frame=nibe.read_request(47134))
    ((_, error_bytes),) = control.handle(p.encode(request), A, 10)
    error = p.decode(error_bytes).message
    assert isinstance(error, p.Error)
    assert error.code == p.ErrorCode.AUTH_FAILED
    assert p.verify(error_bytes, key) == 2  # inside the session, errors are signed too

    ((_, fate_bytes),) = control.handle(p.encode(request, key=key, seq=1), A, 2_000_000)
    assert p.verify(fate_bytes, key) == 3
    assert engine.depths()[READ_KEY] == 1

    replay = only(send(control, A, request, 4_000_000, key=key, seq=1))
    assert isinstance(replay, p.Error)
    assert replay.code == p.ErrorCode.REPLAY
