from thermaestro_gateway import nibe
from thermaestro_gateway import protocol as p
from thermaestro_gateway.bus import Exchange, Reply
from thermaestro_gateway.engine import Engine, Outgoing

READ_KEY = (nibe.MODBUS40, nibe.READ_TOKEN)
WRITE_KEY = (nibe.MODBUS40, nibe.WRITE_TOKEN)
READ_TOKEN = bytes.fromhex("5c0020690049")
WRITE_TOKEN = bytes.fromhex("5c00206b004b")
ACCESSORY_TOKEN = bytes.fromhex("5c0020ee00ce")


def engine(constants: dict[tuple[int, int], bytes] | None = None) -> Engine:
    return Engine(keys={READ_KEY, WRITE_KEY}, constants=constants)


def read(
    request_id: int,
    register: int = 47134,
    *,
    flags: int = p.RequestFlag.EXPECT_ANSWER,
    ttl_ms: int = 0,
    answer_timeout_ms: int = 0,
) -> p.Request:
    return p.Request(
        id=request_id,
        address=nibe.MODBUS40,
        token=nibe.READ_TOKEN,
        flags=flags,
        ttl_ms=ttl_ms,
        answer_timeout_ms=answer_timeout_ms,
        frame=nibe.read_request(register),
    )


def write(request_id: int, register: int = 47387, value: int = 1) -> p.Request:
    return p.Request(
        id=request_id,
        address=nibe.MODBUS40,
        token=nibe.WRITE_TOKEN,
        flags=p.RequestFlag.EXPECT_ANSWER,
        frame=nibe.write_request(register, value),
    )


def fates(out: list[Outgoing]) -> list[tuple[object, int, int, int]]:
    return [
        (o.client, o.message.id, o.message.stage, o.message.detail)
        for o in out
        if isinstance(o.message, p.Fate)
    ]


def answers(out: list[Outgoing]) -> list[tuple[object, int, int]]:
    return [
        (o.client, o.message.id, o.message.status) for o in out if isinstance(o.message, p.Answer)
    ]


def token_exchange(
    e: Engine, token: bytes, now_us: int, trailer: int | None = nibe.ACK
) -> Exchange:
    """The pump sends a token; the engine answers; the pump closes with `trailer`."""
    command = token[3]
    reply = e.reply_for(nibe.MODBUS40, command, now_us)
    data = (
        token
        + (reply.frame if reply else b"")
        + (bytes((trailer,)) if trailer is not None else b"")
    )
    x = Exchange(
        data=data,
        kind=p.FrameKind.TO_GATEWAY,
        telegram=nibe.parse_telegram(token),
        reply=reply,
        trailer=trailer if reply else nibe.ACK,
        t_complete_us=now_us + 10_000,
        t_reply_us=now_us if reply else None,
    )
    e.on_exchange(x)
    return x


def data_exchange(e: Engine, telegram: bytes, now_us: int) -> None:
    e.on_exchange(
        Exchange(
            data=telegram + b"\x06",
            kind=p.FrameKind.TO_GATEWAY,
            telegram=nibe.parse_telegram(telegram),
            reply=None,
            trailer=nibe.ACK,
            t_complete_us=now_us,
        )
    )


def read_answer(register: int, value: int) -> bytes:
    payload = register.to_bytes(2, "little") + value.to_bytes(2, "little") + b"\x00\x00"
    body = bytes((0x00, 0x20, nibe.READ_ANSWER, len(payload))) + payload
    return b"\x5c" + body + bytes((nibe.checksum(body),))


def write_answer(result: int) -> bytes:
    body = bytes((0x00, 0x20, nibe.WRITE_ANSWER, 1, result))
    return b"\x5c" + body + bytes((nibe.checksum(body),))


# --- submitting --------------------------------------------------------------------------


def test_a_valid_request_is_queued() -> None:
    e = engine()
    e.submit("a", read(1), 0)
    e.submit("a", read(2, register=40004), 0)
    assert fates(e.take_outbox()) == [
        ("a", 1, p.Stage.QUEUED, 0),
        ("a", 2, p.Stage.QUEUED, 1),
    ]


def test_invalid_requests_are_dropped_with_a_reason() -> None:
    e = engine()
    bad_frame = p.Request(id=1, address=0x20, token=0x69, frame=b"\xc0\x69\x02\x1e\xb8\x00")
    mismatch = p.Request(id=2, address=0x20, token=0x69, frame=nibe.write_request(47387, 1))
    unknown = p.Request(id=3, address=0x19, token=0x60, frame=bytes.fromhex("c06000a0"))
    for r in (bad_frame, mismatch, unknown):
        e.submit("a", r, 0)
    assert fates(e.take_outbox()) == [
        ("a", 1, p.Stage.DROPPED, p.DropReason.INVALID_FRAME),
        ("a", 2, p.Stage.DROPPED, p.DropReason.TOKEN_MISMATCH),
        ("a", 3, p.Stage.DROPPED, p.DropReason.UNKNOWN_KEY),
    ]


def test_a_full_queue_refuses_protocol_requests() -> None:
    e = engine()
    for i in range(4):
        e.submit("a", read(i + 1, register=40000 + i), 0)
    assert fates(e.take_outbox())[-1] == ("a", 4, p.Stage.DROPPED, p.DropReason.QUEUE_FULL)
    assert e.stats.drops[p.DropReason.QUEUE_FULL] == 1


def test_a_plain_request_evicts_the_oldest_even_a_protocol_one() -> None:
    e = engine()
    for i in range(3):
        e.submit("a", read(i + 1, register=40000 + i), 0)
    e.take_outbox()
    assert e.submit_plain(nibe.MODBUS40, nibe.READ_TOKEN, nibe.read_request(40010), 0)
    assert fates(e.take_outbox()) == [("a", 1, p.Stage.DROPPED, p.DropReason.EVICTED)]
    assert e.stats.evictions == 1


def test_priority_goes_to_the_front() -> None:
    e = engine()
    e.submit("a", read(1), 0)
    e.submit("a", read(2, register=40004, flags=p.RequestFlag.PRIORITY), 0)
    e.take_outbox()
    reply = e.reply_for(nibe.MODBUS40, nibe.READ_TOKEN, 1)
    assert reply is not None
    assert reply.frame == nibe.read_request(40004)


# --- on the bus --------------------------------------------------------------------------


def test_sent_then_acked_by_the_pump() -> None:
    e = engine()
    e.submit("a", read(1), 0)
    e.take_outbox()
    token_exchange(e, READ_TOKEN, 1_000_000)
    out = e.take_outbox()
    assert fates(out) == [("a", 1, p.Stage.SENT, 0), ("a", 1, p.Stage.PUMP_ACK, 0)]
    sent = next(o.message for o in out if isinstance(o.message, p.Fate))
    assert sent.stage_time_us == 1_000_000


def test_pump_nak_and_no_ack_seen() -> None:
    e = engine()
    e.submit("a", read(1), 0)
    e.submit("a", read(2, register=40004), 0)
    e.take_outbox()
    token_exchange(e, READ_TOKEN, 1_000, trailer=nibe.NAK)
    token_exchange(e, READ_TOKEN, 2_000, trailer=None)
    stages = [(f[1], f[2]) for f in fates(e.take_outbox())]
    assert (1, p.Stage.PUMP_NAK) in stages
    assert (2, p.Stage.NO_ACK_SEEN) in stages


def test_an_empty_queue_gets_an_ack_or_a_constant() -> None:
    e = engine(constants={(nibe.MODBUS40, 0xEE): bytes((0x0A, 0x00, 0x01))})
    assert e.reply_for(nibe.MODBUS40, nibe.READ_TOKEN, 0) is None
    reply = e.reply_for(nibe.MODBUS40, 0xEE, 0)
    assert reply is not None
    nibe.validate_reply(reply.frame)
    assert reply.frame[:6] == bytes((0xC0, 0xEE, 0x03, 0x0A, 0x00, 0x01))


def test_an_expired_request_is_dropped_when_its_token_comes() -> None:
    e = engine()
    e.submit("a", read(1, ttl_ms=100), 0)
    e.submit("a", read(2, register=40004), 0)
    e.take_outbox()
    reply = e.reply_for(nibe.MODBUS40, nibe.READ_TOKEN, 200_000)
    assert reply is not None
    assert reply.frame == nibe.read_request(40004)
    assert ("a", 1, p.Stage.DROPPED, p.DropReason.EXPIRED) in fates(e.take_outbox())


def test_cancel() -> None:
    e = engine()
    e.submit("a", read(1), 0)
    e.take_outbox()
    assert e.cancel("a", 1, 10)
    assert not e.cancel("a", 1, 10)
    assert not e.cancel("b", 99, 10)
    assert fates(e.take_outbox()) == [("a", 1, p.Stage.DROPPED, p.DropReason.CANCELLED)]
    assert e.reply_for(nibe.MODBUS40, nibe.READ_TOKEN, 20) is None


def test_forgetting_a_client_removes_its_requests_silently() -> None:
    e = engine()
    e.submit("a", read(1), 0)
    e.submit("b", read(2, register=40004), 0)
    e.take_outbox()
    e.forget("a")
    reply = e.reply_for(nibe.MODBUS40, nibe.READ_TOKEN, 1)
    assert reply is not None
    assert reply.frame == nibe.read_request(40004)
    assert fates(e.take_outbox()) == [("b", 2, p.Stage.SENT, 0)]


# --- answers -----------------------------------------------------------------------------


def test_read_answers_pair_first_in_first_out_per_register() -> None:
    e = engine()
    e.submit("a", read(1), 0)
    e.submit("b", read(7), 0)
    e.take_outbox()
    token_exchange(e, READ_TOKEN, 1_000)
    token_exchange(e, READ_TOKEN, 2_000)
    e.take_outbox()
    data_exchange(e, read_answer(47134, 45), 1_000_000)
    out = e.take_outbox()
    assert answers(out) == [("a", 1, p.AnswerStatus.OK)]
    answer = next(o.message for o in out if isinstance(o.message, p.Answer))
    assert answer.frame == read_answer(47134, 45)
    data_exchange(e, read_answer(47134, 46), 1_001_000)
    assert answers(e.take_outbox()) == [("b", 7, p.AnswerStatus.OK)]


def test_a_read_back_doesnt_get_the_answer_to_a_plain_read_taken_before_it() -> None:
    # A plain client's read taken before a write is answered after the write;
    # that answer belongs to the plain read, not to the read-back taken later.
    e = engine()
    assert e.submit_plain(nibe.MODBUS40, nibe.READ_TOKEN, nibe.read_request(47387), 0)
    token_exchange(e, READ_TOKEN, 1_000)
    e.submit("a", read(1, register=47387), 2_000)
    token_exchange(e, READ_TOKEN, 3_000)
    e.take_outbox()
    data_exchange(e, read_answer(47387, 1), 1_000_000)
    assert answers(e.take_outbox()) == []
    data_exchange(e, read_answer(47387, 0), 1_003_000)
    assert answers(e.take_outbox()) == [("a", 1, p.AnswerStatus.OK)]


def test_a_nakked_read_waits_for_no_answer() -> None:
    e = engine()
    e.submit("a", read(1), 0)
    e.submit("b", read(2), 0)
    e.take_outbox()
    token_exchange(e, READ_TOKEN, 1_000, trailer=nibe.NAK)
    token_exchange(e, READ_TOKEN, 2_000)
    e.take_outbox()
    data_exchange(e, read_answer(47134, 45), 1_000_000)
    assert answers(e.take_outbox()) == [("b", 2, p.AnswerStatus.OK)]


def test_a_read_that_timed_out_leaves_the_list() -> None:
    e = engine()
    e.submit("a", read(1, answer_timeout_ms=1000), 0)
    e.take_outbox()
    token_exchange(e, READ_TOKEN, 0)
    e.tick(1_100_000)
    assert answers(e.take_outbox()) == [("a", 1, p.AnswerStatus.TIMEOUT)]
    e.submit("b", read(2), 1_200_000)
    token_exchange(e, READ_TOKEN, 1_300_000)
    e.take_outbox()
    data_exchange(e, read_answer(47134, 45), 2_300_000)
    assert answers(e.take_outbox()) == [("b", 2, p.AnswerStatus.OK)]


def test_an_overdue_read_times_out_when_an_answer_comes_before_the_tick() -> None:
    e = engine()
    e.submit("a", read(1, answer_timeout_ms=1000), 0)
    e.submit("b", read(2), 0)
    e.take_outbox()
    token_exchange(e, READ_TOKEN, 0)
    token_exchange(e, READ_TOKEN, 500_000)
    e.take_outbox()
    data_exchange(e, read_answer(47134, 45), 1_500_000)
    assert answers(e.take_outbox()) == [
        ("a", 1, p.AnswerStatus.TIMEOUT),
        ("b", 2, p.AnswerStatus.OK),
    ]


def test_a_forgotten_clients_read_keeps_its_place() -> None:
    e = engine()
    e.submit("a", read(1), 0)
    e.submit("b", read(2), 0)
    e.take_outbox()
    token_exchange(e, READ_TOKEN, 1_000)
    token_exchange(e, READ_TOKEN, 2_000)
    e.take_outbox()
    e.forget("a")
    data_exchange(e, read_answer(47134, 45), 1_000_000)
    assert answers(e.take_outbox()) == []
    data_exchange(e, read_answer(47134, 46), 1_001_000)
    assert answers(e.take_outbox()) == [("b", 2, p.AnswerStatus.OK)]


def test_an_answer_for_another_register_isnt_paired() -> None:
    e = engine()
    e.submit("a", read(1), 0)
    e.take_outbox()
    token_exchange(e, READ_TOKEN, 1_000)
    e.take_outbox()
    data_exchange(e, read_answer(40004, 12), 1_000_000)
    assert answers(e.take_outbox()) == []


def test_no_answer_in_time() -> None:
    e = engine()
    e.submit("a", read(1, answer_timeout_ms=2000), 0)
    e.take_outbox()
    token_exchange(e, READ_TOKEN, 1_000)
    e.take_outbox()
    e.tick(1_500_000)
    assert answers(e.take_outbox()) == []
    e.tick(2_100_000)
    assert answers(e.take_outbox()) == [("a", 1, p.AnswerStatus.TIMEOUT)]
    assert e.stats.answer_timeouts == 1


def test_one_protocol_write_in_flight() -> None:
    e = engine()
    e.submit("a", write(1), 0)
    e.submit("a", write(2, value=0), 0)
    e.take_outbox()
    token_exchange(e, WRITE_TOKEN, 1_000)
    # The first write awaits its 0x6C, so the second isn't sent on the next token.
    assert e.reply_for(nibe.MODBUS40, nibe.WRITE_TOKEN, 2_000) is None
    e.take_outbox()
    data_exchange(e, write_answer(1), 500_000)
    assert answers(e.take_outbox()) == [("a", 1, p.AnswerStatus.OK)]
    reply = e.reply_for(nibe.MODBUS40, nibe.WRITE_TOKEN, 600_000)
    assert reply is not None
    assert reply.frame == nibe.write_request(47387, 0)


def test_a_plain_write_in_flight_makes_the_pairing_ambiguous() -> None:
    e = engine()
    e.submit("a", write(1), 0)
    e.take_outbox()
    token_exchange(e, WRITE_TOKEN, 1_000)  # protocol write sent
    e.submit_plain(nibe.MODBUS40, nibe.WRITE_TOKEN, nibe.write_request(47388, 2), 2_000)
    token_exchange(e, WRITE_TOKEN, 3_000)  # plain write sent too: two in flight
    e.take_outbox()
    data_exchange(e, write_answer(1), 500_000)
    assert answers(e.take_outbox()) == [("a", 1, p.AnswerStatus.AMBIGUOUS)]
    assert e.stats.ambiguous_answers == 1


def test_a_nakked_write_isnt_in_flight() -> None:
    e = engine()
    e.submit("a", write(1), 0)
    e.submit("a", write(2, value=0), 0)
    e.take_outbox()
    token_exchange(e, WRITE_TOKEN, 1_000, trailer=nibe.NAK)
    assert e.reply_for(nibe.MODBUS40, nibe.WRITE_TOKEN, 2_000) is not None


def test_the_accessory_token_isnt_a_queue() -> None:
    e = engine()
    assert e.reply_for(nibe.MODBUS40, nibe.parse_telegram(ACCESSORY_TOKEN).command, 0) is None


def test_queue_depths() -> None:
    e = engine()
    e.submit("a", read(1), 0)
    assert e.depths() == {READ_KEY: 1, WRITE_KEY: 0}


def test_reply_is_tagged_with_the_queued_entry() -> None:
    e = engine()
    e.submit("a", read(1), 0)
    reply = e.reply_for(nibe.MODBUS40, nibe.READ_TOKEN, 0)
    assert isinstance(reply, Reply)
    assert e.owner_of(reply) == ("a", 1)
    plain = Reply(b"", ref=None)
    assert e.owner_of(plain) is None
