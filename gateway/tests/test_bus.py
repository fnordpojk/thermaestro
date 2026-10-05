from dataclasses import dataclass, field

from thermaestro_gateway import nibe
from thermaestro_gateway.bus import Bus, Exchange, Reply
from thermaestro_gateway.protocol import FrameKind

READ_TOKEN = bytes.fromhex("5c0020690049")
WRITE_TOKEN = bytes.fromhex("5c00206b004b")
READ_47134 = bytes.fromhex("c069021eb80d")
ANSWER = bytes.fromhex("5c00206a061eb82d000000c7")
RMU_TOKEN = bytes.fromhex("5c0019600079")


@dataclass
class Responder:
    """Answers tokens from a list, as the gateway's queues would."""

    replies: list[Reply] = field(default_factory=list)
    asked: list[tuple[int, int]] = field(default_factory=list)

    def reply_for(self, address: int, command: int, now_us: int) -> Reply | None:
        self.asked.append((address, command))
        return self.replies.pop(0) if self.replies else None


@dataclass
class Run:
    written: bytes = b""
    done: list[Exchange] = field(default_factory=list)


def feed(bus: Bus, data: bytes, start_us: int = 0, run: Run | None = None) -> Run:
    run = run or Run()
    for i, b in enumerate(data):
        step = bus.feed(b, start_us + i * 1000)
        run.written += step.write
        if step.done is not None:
            run.done.append(step.done)
    return run


def make_bus(*replies: Reply) -> tuple[Bus, Responder]:
    responder = Responder(list(replies))
    return Bus(acknowledged=frozenset({nibe.MODBUS40}), responder=responder), responder


def test_token_answered_with_a_request_then_pump_acks() -> None:
    bus, responder = make_bus(Reply(READ_47134, ref="r1"))
    run = feed(bus, READ_TOKEN)
    assert run.written == READ_47134
    assert run.done == []
    feed(bus, b"\x06", start_us=10_000, run=run)
    (x,) = run.done
    assert x.data == READ_TOKEN + READ_47134 + b"\x06"
    assert x.kind == FrameKind.TO_GATEWAY
    assert x.reply == Reply(READ_47134, ref="r1")
    assert x.trailer == nibe.ACK
    assert x.t_reply_us == 5000
    assert x.t_complete_us == 10_000
    assert responder.asked == [(nibe.MODBUS40, nibe.READ_TOKEN)]
    assert bus.stats.tokens_with_reply == 1


def test_token_with_nothing_queued_gets_an_ack() -> None:
    bus, _ = make_bus()
    run = feed(bus, READ_TOKEN)
    assert run.written == b"\x06"
    (x,) = run.done
    assert x.data == READ_TOKEN + b"\x06"
    assert x.reply is None
    assert bus.stats.tokens_ack_only == 1


def test_data_telegram_to_us_gets_an_ack() -> None:
    bus, responder = make_bus()
    run = feed(bus, ANSWER)
    assert run.written == b"\x06"
    (x,) = run.done
    assert x.telegram is not None
    assert x.telegram.command == nibe.READ_ANSWER
    assert x.data == ANSWER + b"\x06"
    assert responder.asked == []


def test_bad_checksum_to_us_gets_a_nak() -> None:
    bus, _ = make_bus()
    run = feed(bus, ANSWER[:-1] + b"\x00")
    assert run.written == b"\x15"
    (x,) = run.done
    assert x.kind == FrameKind.UNPARSED
    assert x.telegram is None
    assert (bus.stats.crc_errors, bus.stats.naks_sent) == (1, 1)


def test_telegram_to_another_device_is_followed_not_answered() -> None:
    rmu_reply = bytes.fromhex("c06003061400") + bytes(
        (nibe.checksum(bytes.fromhex("c06003061400")),)
    )
    bus, responder = make_bus()
    run = feed(bus, RMU_TOKEN + rmu_reply + b"\x06")
    assert run.written == b""
    (x,) = run.done
    assert x.kind == FrameKind.TO_OTHER
    assert x.data == RMU_TOKEN + rmu_reply + b"\x06"
    assert x.reply is None
    assert responder.asked == []


def test_other_device_answering_with_an_ack_only() -> None:
    bus, _ = make_bus()
    run = feed(bus, RMU_TOKEN + b"\x06")
    (x,) = run.done
    assert (x.kind, x.data) == (FrameKind.TO_OTHER, RMU_TOKEN + b"\x06")


def test_next_telegram_instead_of_an_ack() -> None:
    bus, _ = make_bus(Reply(READ_47134))
    run = feed(bus, READ_TOKEN + WRITE_TOKEN)
    first, second = run.done
    assert first.data == READ_TOKEN + READ_47134
    assert first.trailer is None
    assert bus.stats.no_ack_seen == 1
    assert second.telegram is not None
    assert second.telegram.command == nibe.WRITE_TOKEN


def test_pump_naks_our_reply() -> None:
    bus, _ = make_bus(Reply(READ_47134))
    run = feed(bus, READ_TOKEN + b"\x15")
    (x,) = run.done
    assert x.trailer == nibe.NAK
    assert bus.stats.pump_naks == 1


def test_a_doubled_start_byte_is_data_not_a_start() -> None:
    bus, _ = make_bus()
    run = feed(bus, b"\x5c" + READ_TOKEN)  # 5C 5C 00 20 ...: the pair is an escaped data byte
    assert run.done == []
    feed(bus, READ_TOKEN, start_us=100_000, run=run)
    assert len(run.done) == 1


def test_an_unpaired_start_byte_after_a_pair_starts_a_telegram() -> None:
    bus, _ = make_bus()
    run = feed(bus, b"\x5c\x5c" + READ_TOKEN)
    assert len(run.done) == 1


def test_bytes_before_a_start_are_counted() -> None:
    bus, _ = make_bus()
    run = feed(bus, b"\x01\x02" + READ_TOKEN)
    assert len(run.done) == 1
    assert bus.stats.invalid_bytes == 2


def test_doubled_start_byte_inside_data_doesnt_restart() -> None:
    bus, _ = make_bus()
    escaped = bytes.fromhex("5c00206a071eb85c5c000000eb")
    run = feed(bus, escaped)
    (x,) = run.done
    assert x.telegram is not None
    assert x.telegram.payload == bytes.fromhex("1eb85c000000")


def test_invalid_reply_from_the_responder_is_not_sent() -> None:
    bad = READ_47134[:-1] + b"\x00"
    bus, _ = make_bus(Reply(bad, ref="bad"))
    run = feed(bus, READ_TOKEN)
    assert run.written == b"\x06"
    (x,) = run.done
    assert x.reply is None
    assert x.refused == Reply(bad, ref="bad")


def test_reply_for_the_wrong_token_is_not_sent() -> None:
    bus, _ = make_bus(Reply(READ_47134, ref="read"))
    run = feed(bus, WRITE_TOKEN)
    assert run.written == b"\x06"
    (x,) = run.done
    assert x.refused == Reply(READ_47134, ref="read")


def test_last_byte_and_token_times() -> None:
    bus, _ = make_bus()
    feed(bus, READ_TOKEN, start_us=1_000_000)
    assert bus.stats.last_byte_us == 1_005_000
    assert bus.stats.last_token_us == 1_005_000
