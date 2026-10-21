"""The dead-man: what a silent client changed is put back, and only where nobody else has
changed it since (docs/gateway-protocol.md §15)."""

import json
from pathlib import Path

from thermaestro_gateway import nibe
from thermaestro_gateway import protocol as p
from thermaestro_gateway.bus import BusStats, Exchange
from thermaestro_gateway.control import Control, Settings
from thermaestro_gateway.deadman import SAVE_EVERY_US

A = ("192.0.2.10", 40000)
B = ("192.0.2.10", 40001)
PSK = bytes(range(32))
NONCE = bytes(range(0x10, 0x20))
BOOT = 0xDEADBEEF
S = 1_000_000
OFFSET = 47011
SET, FOUND = 0xFFFE, 0xFFFC  # Thermaestro's -2, the household's -4


def make(state: Path | None = None, *, psk: bytes | None = PSK) -> Control:
    from thermaestro_gateway.engine import Engine

    engine = Engine(keys={(nibe.MODBUS40, nibe.READ_TOKEN), (nibe.MODBUS40, nibe.WRITE_TOKEN)})
    settings = Settings(psk=psk, boot_id=BOOT, plain_ports=(9999, 0), deadman_state=state)
    return Control(engine, BusStats(), settings)


class Client:
    """An authenticated session, with its signing state."""

    def __init__(self, control: Control, addr: tuple[str, int], now_us: int, *options: p.Option):
        self.control, self.addr = control, addr
        hello = p.Hello(id=1, options=(p.Option(p.Tag.CLIENT_NONCE | p.CRITICAL, NONCE), *options))
        out = control.handle(p.encode(hello, key=PSK, seq=1), addr, now_us)
        welcome = p.decode(out[0][1]).message
        assert isinstance(welcome, p.Welcome)
        self.welcome = welcome
        gateway_nonce = p.find(welcome.options, p.Tag.GATEWAY_NONCE)
        assert gateway_nonce is not None
        self.key = p.session_key(PSK, NONCE, gateway_nonce.value, BOOT)
        self.seq = 1
        self.after_welcome = [p.decode(d).message for _, d in out[1:]]

    def send(self, msg: p.Message, now_us: int) -> list[p.Message]:
        self.seq += 1
        out = self.control.handle(p.encode(msg, key=self.key, seq=self.seq), self.addr, now_us)
        return [p.decode(d).message for to, d in out if to == self.addr]


def asking(seconds: int = 900, entries: int = 16) -> p.Option:
    return p.Option.pack(p.Tag.DEADMAN, seconds, entries, 0, 0)


def arm(entry: int = 0, register: int = OFFSET, expected: int = SET, put: int = FOUND) -> p.Arm:
    return p.Arm(
        id=10 + entry,
        entry=entry,
        width=2,
        register=register,
        expected=expected,
        frame=nibe.write_request(register, put),
    )


def telegram(command: int, payload: bytes) -> bytes:
    body = nibe.MODBUS40.to_bytes(2, "big") + bytes((command, len(payload))) + payload
    return bytes((nibe.START_TELEGRAM,)) + body + bytes((nibe.checksum(body),))


class Pump:
    """The pump's side of the bus: tokens, and the answers to what it took."""

    def __init__(self, control: Control, registers: dict[int, int]) -> None:
        self.control = control
        self.registers = registers
        self.writes: list[tuple[int, int]] = []

    def cycle(
        self, now_us: int, tokens: tuple[int, ...] = (nibe.READ_TOKEN, nibe.WRITE_TOKEN)
    ) -> None:
        engine = self.control.engine
        for token in tokens:
            reply = engine.reply_for(nibe.MODBUS40, token, now_us)
            token_telegram = telegram(token, b"")
            self.control.on_exchange(
                Exchange(
                    data=token_telegram + (reply.frame if reply else b"") + b"\x06",
                    kind=p.FrameKind.TO_GATEWAY,
                    telegram=nibe.parse_telegram(token_telegram),
                    reply=reply,
                    trailer=nibe.ACK,
                    t_complete_us=now_us,
                    t_reply_us=now_us if reply else None,
                )
            )
            if reply is None:
                continue
            register = int.from_bytes(reply.frame[3:5], "little")
            if token == nibe.READ_TOKEN:
                value = self.registers.get(register, 0)
                answer = telegram(
                    nibe.READ_ANSWER, register.to_bytes(2, "little") + value.to_bytes(4, "little")
                )
            else:
                value = int.from_bytes(reply.frame[5:9], "little") & 0xFFFF
                self.registers[register] = value
                self.writes.append((register, value))
                answer = telegram(nibe.WRITE_ANSWER, b"\x01")
            self.control.on_exchange(
                Exchange(
                    data=answer + b"\x06",
                    kind=p.FrameKind.TO_GATEWAY,
                    telegram=nibe.parse_telegram(answer),
                    reply=None,
                    trailer=nibe.ACK,
                    t_complete_us=now_us + 1000,
                )
            )

    def run(self, start_us: int, until_us: int, step_us: int = S) -> None:
        t = start_us
        while t <= until_us:
            self.control.tick(t)
            self.cycle(t)
            t += step_us


def test_granted_only_with_a_key_and_when_asked() -> None:
    control = make()
    granted = Client(control, A, 0, asking(60, 40))
    option = p.find(granted.welcome.options, p.Tag.DEADMAN)
    assert option is not None
    assert option.unpack() == (300, 16, 0, 0)  # at least five minutes, at most 16 entries
    features = p.find(granted.welcome.options, p.Tag.FEATURES)
    assert features is not None
    assert p.Feature.DEADMAN in p.Feature(features.as_int())
    plain = Client(control, B, 0)
    assert p.find(plain.welcome.options, p.Tag.DEADMAN) is None
    (refused,) = plain.send(arm(), 1)
    assert isinstance(refused, p.Armed)
    assert (refused.result, refused.detail) == (p.ArmResult.REFUSED, p.ArmRefusal.NOT_GRANTED)
    keyless = make(psk=None)
    out = keyless.handle(p.encode(p.Hello(id=1, options=(asking(),))), A, 0)
    welcome = p.decode(out[0][1]).message
    assert p.find(welcome.options, p.Tag.DEADMAN) is None
    assert keyless.deadman is None


def test_what_arm_refuses() -> None:
    client = Client(make(), A, 0, asking(entries=4))
    cases = [
        (arm(entry=4), p.ArmRefusal.NO_SUCH_ENTRY),
        (
            p.Arm(entry=0, width=3, register=OFFSET, expected=SET, frame=arm().frame),
            p.ArmRefusal.BAD_WIDTH,
        ),
        (
            p.Arm(entry=0, width=2, register=OFFSET, expected=SET, frame=nibe.read_request(OFFSET)),
            p.ArmRefusal.INVALID_FRAME,
        ),
        (
            p.Arm(entry=0, width=2, register=47012, expected=SET, frame=arm().frame),
            p.ArmRefusal.REGISTER_MISMATCH,
        ),
    ]
    for msg, why in cases:
        (answer,) = client.send(msg, 1)
        assert isinstance(answer, p.Armed)
        assert (answer.result, answer.detail) == (p.ArmResult.REFUSED, why)
    (armed,) = client.send(arm(), 2)
    assert isinstance(armed, p.Armed)
    assert armed.result == p.ArmResult.ARMED


def test_a_silent_client_has_its_change_put_back_and_hears_of_it() -> None:
    control = make()
    client = Client(control, A, 0, asking())
    client.send(arm(), 1 * S)
    pump = Pump(control, {OFFSET: SET})
    pump.run(2 * S, 899 * S, step_us=60 * S)
    assert pump.writes == []  # still within its silence
    pump.run(902 * S, 910 * S)
    assert pump.writes == [(OFFSET, FOUND)]
    back = Client(control, A, 1000 * S, asking())
    option = p.find(back.welcome.options, p.Tag.DEADMAN)
    assert option is not None
    assert option.unpack() == (900, 16, 0, 1)
    (fired,) = back.after_welcome
    assert isinstance(fired, p.Fired)
    assert (fired.entry, fired.outcome, fired.register) == (0, p.FiredOutcome.PUT_BACK, OFFSET)
    assert fired.fired_us > 900 * S
    (cleared,) = back.send(p.AckFired(id=7, entry=0), 1001 * S)
    assert isinstance(cleared, p.Armed)
    assert cleared.result == p.ArmResult.DISARMED
    again = Client(control, A, 1002 * S, asking())
    assert again.after_welcome == []
    pump.run(1003 * S, 3000 * S, step_us=60 * S)
    assert pump.writes == [(OFFSET, FOUND)]  # once only


def test_a_change_made_by_someone_else_is_never_written_over() -> None:
    control = make()
    client = Client(control, A, 0, asking())
    client.send(arm(), 1 * S)
    pump = Pump(control, {OFFSET: 0x0001})  # set to +1 at the pump while Thermaestro was away
    pump.run(901 * S, 910 * S)
    assert pump.writes == []
    assert pump.registers[OFFSET] == 0x0001
    (fired,) = Client(control, A, 1000 * S, asking()).after_welcome
    assert isinstance(fired, p.Fired)
    assert (fired.outcome, fired.read) == (p.FiredOutcome.SKIPPED, 0x0001)


def test_any_message_from_the_client_keeps_it_from_firing_and_disarm_ends_it() -> None:
    control = make()
    client = Client(control, A, 0, asking())
    client.send(arm(), 1 * S)
    pump = Pump(control, {OFFSET: SET})
    for t in range(600, 3000, 600):
        client.send(p.Keepalive(), t * S)
        pump.run(t * S, t * S + 10 * S)
    assert pump.writes == []
    client.send(p.Disarm(entry=0), 3000 * S)
    pump.run(3001 * S, 6000 * S, step_us=60 * S)
    assert pump.writes == []


def test_a_client_back_meanwhile_stops_the_firing_after_the_entry_under_way() -> None:
    control = make()
    client = Client(control, A, 0, asking())
    client.send(arm(0), 1 * S)
    client.send(arm(1, register=47376, expected=0xFF6A, put=0x0096), 1 * S)
    pump = Pump(control, {OFFSET: SET, 47376: 0xFF6A})
    control.tick(901 * S)  # entry 0's read is queued
    back = Client(control, A, 901 * S + 1, asking())
    assert back.welcome is not None
    pump.run(902 * S, 920 * S)
    assert pump.writes == [(OFFSET, FOUND)]
    assert set(control.deadman.armed) == {1}  # type: ignore[union-attr]


def test_kept_through_a_restart_of_the_gateway(tmp_path: Path) -> None:
    state = tmp_path / "deadman.json"
    control = make(state)
    Client(control, A, 0, asking()).send(arm(), 1 * S)
    control.tick(2 * S)
    assert not state.exists()  # saved now and then, not on every change
    control.tick(SAVE_EVERY_US + 2 * S)
    assert json.loads(state.read_text())["armed"][0]["register"] == OFFSET
    restarted = make(state)  # the silence counts from the restart
    pump = Pump(restarted, {OFFSET: SET})
    pump.run(0, 899 * S, step_us=60 * S)
    assert pump.writes == []
    pump.run(901 * S, 910 * S)
    assert pump.writes == [(OFFSET, FOUND)]
    # Fired and kept at once: a second restart reports it, and doesn't fire it again.
    again = make(state)
    pump = Pump(again, {OFFSET: SET})
    pump.run(901 * S, 910 * S)
    assert pump.writes == []
    (fired,) = Client(again, A, 1000 * S, asking()).after_welcome
    assert isinstance(fired, p.Fired)
    assert (fired.outcome, fired.fired_us) == (p.FiredOutcome.PUT_BACK, 0)


def test_stopping_mid_firing_keeps_the_entry_armed(tmp_path: Path) -> None:
    state = tmp_path / "deadman.json"
    control = make(state)
    Client(control, A, 0, asking()).send(arm(), 1 * S)
    control.tick(901 * S)  # its read is queued
    control.shutdown(902 * S)
    assert [e["number"] for e in json.loads(state.read_text())["armed"]] == [0]


def firing_saved(tmp_path: Path, *, past_read: bool) -> tuple[Path, Pump]:
    """Armed and silent for 300 s, so fired at 599 s; saved at 600 s, ten minutes after the
    gateway started, while its read (or, with `past_read`, its write) is still out; then
    the gateway gone without a word."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    state = tmp_path / "deadman.json"
    control = make(state)
    Client(control, A, 0, asking(300)).send(arm(), 299 * S)
    pump = Pump(control, {OFFSET: SET})
    control.tick(599 * S)  # the read is queued
    if past_read:
        pump.cycle(599 * S + 500_000, tokens=(nibe.READ_TOKEN,))  # answered: the write is queued
    control.tick(SAVE_EVERY_US)
    return state, pump


def test_a_restart_during_a_firing_fires_it_again(tmp_path: Path) -> None:
    for past_read in (False, True):
        state, before = firing_saved(tmp_path / str(past_read), past_read=past_read)
        assert before.writes == []
        restarted = make(state)
        pump = Pump(restarted, {OFFSET: SET})  # still as the client left it
        pump.run(0, 310 * S)
        assert pump.writes == [(OFFSET, FOUND)], past_read
        (fired,) = Client(restarted, A, 1000 * S, asking()).after_welcome
        assert isinstance(fired, p.Fired)
        assert fired.outcome == p.FiredOutcome.PUT_BACK


def test_after_a_restart_a_register_already_back_or_changed_isnt_written(tmp_path: Path) -> None:
    for value, outcome in ((FOUND, p.FiredOutcome.PUT_BACK), (0x0001, p.FiredOutcome.SKIPPED)):
        state, _ = firing_saved(tmp_path / str(value), past_read=True)
        restarted = make(state)
        # The write before the restart went through, or someone set the register meanwhile.
        pump = Pump(restarted, {OFFSET: value})
        pump.run(0, 310 * S)
        assert pump.writes == []
        assert pump.registers[OFFSET] == value
        (fired,) = Client(restarted, A, 1000 * S, asking()).after_welcome
        assert isinstance(fired, p.Fired)
        assert (fired.outcome, fired.read) == (outcome, value)
