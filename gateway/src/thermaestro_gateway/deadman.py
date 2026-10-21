"""The dead-man: put back what a silent client changed (docs/gateway-protocol.md §15).

An authenticated client arms entries: a register, the value it wrote, and the write that
puts the register's earlier value back. Once no session granted the dead-man has said
anything for its silence, each armed entry is fired in turn: the gateway reads the
register, and only where it still holds what the client wrote does it send the write.
Someone who changed it meanwhile is never written over. Each entry fires once; what came
of it is kept until a session acknowledges it.

It is a client of the engine like any other: its reads and writes wait in the same
queues, and a write waits for the one in flight. There is no I/O here but an optional
state file, saved now and then, so a reboot of the gateway keeps what was armed.
"""

import contextlib
import json
import logging
import os
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path

from thermaestro_gateway import nibe
from thermaestro_gateway import protocol as p

log = logging.getLogger(__name__)

ENTRIES = 16
SILENCE_S = (300, 900, 7200)  # minimum, default, maximum
SAVE_EVERY_US = 600_000_000
"""How often armed entries that changed are saved, at most."""
REQUEST_TTL_MS = 60_000
FIRST_ID = 0x8000_0000
"""Its own request ids, apart from any client's (they never share an outbox)."""


@dataclass(frozen=True, slots=True)
class Entry:
    number: int
    width: int
    register: int
    expected: int
    frame: bytes


@dataclass(frozen=True, slots=True)
class Outcome:
    entry: int
    outcome: p.FiredOutcome
    register: int
    read: int = 0
    fired_us: int = 0


Submit = Callable[[object, p.Request, int], None]


class DeadMan:
    def __init__(self, submit: Submit, now_us: int, *, state: Path | None = None) -> None:
        self._submit = submit
        self.silence_us = SILENCE_S[1] * 1_000_000
        self.armed: dict[int, Entry] = {}
        self.fired: dict[int, Outcome] = {}
        self._heard_us = now_us
        self._firing = False
        self._queue: list[Entry] = []
        self._current: Entry | None = None
        self._pending: tuple[int, str] | None = None
        """The request in flight for the current entry: its id, and "read" or "write"."""
        self._next_id = FIRST_ID
        self._state = state
        self._dirty = False
        self._saved_us = now_us
        if state is not None:
            self._load(state)

    # --- the client ----------------------------------------------------------------------

    def grant(self, asked_s: int, asked_entries: int) -> tuple[int, int]:
        low, _, high = SILENCE_S
        seconds = min(max(asked_s, low), high)
        if seconds * 1_000_000 != self.silence_us:
            self.silence_us = seconds * 1_000_000
            self._dirty = True
        return seconds, min(max(asked_entries, 0), ENTRIES)

    def heard(self, now_us: int) -> None:
        """A session granted the dead-man spoke: the silence starts again, and a firing
        under way stops after its current entry."""
        self._heard_us = now_us
        if self._firing:
            log.info("dead-man: the client is back, %d entries left armed", len(self._queue))
            self._firing = False
            self._queue = []

    def arm(self, msg: p.Arm, entries: int) -> p.Armed:
        refusal = self._refusal(msg, entries)
        if refusal is not None:
            return p.Armed(entry=msg.entry, result=p.ArmResult.REFUSED, detail=refusal, id=msg.id)
        self.armed[msg.entry] = Entry(msg.entry, msg.width, msg.register, msg.expected, msg.frame)
        self._dirty = True
        return p.Armed(entry=msg.entry, result=p.ArmResult.ARMED, id=msg.id)

    def disarm(self, entry: int, msg_id: int) -> p.Armed:
        if self.armed.pop(entry, None) is not None:
            self._dirty = True
        return p.Armed(entry=entry, result=p.ArmResult.DISARMED, id=msg_id)

    def ack_fired(self, entry: int, msg_id: int) -> p.Armed:
        if self.fired.pop(entry, None) is not None:
            self._dirty = True
        return p.Armed(entry=entry, result=p.ArmResult.DISARMED, id=msg_id)

    def stop(self, now_us: int) -> None:
        """The gateway is stopping: an entry being fired is armed again, and what is armed
        is kept, so a restart fires it."""
        if self._current is not None and self._current.number not in self.armed:
            self.armed[self._current.number] = self._current
        self._current = None
        self._pending = None
        self._firing = False
        self._queue = []
        self.save(now_us)

    def reports(self, now_us: int) -> list[p.Fired]:
        return [
            p.Fired(
                entry=o.entry,
                outcome=o.outcome,
                register=o.register,
                read=o.read,
                fired_us=o.fired_us,
                gw_time_us=now_us,
            )
            for o in sorted(self.fired.values(), key=lambda o: o.entry)
        ]

    @staticmethod
    def _refusal(msg: p.Arm, entries: int) -> p.ArmRefusal | None:
        if not 0 <= msg.entry < entries:
            return p.ArmRefusal.NO_SUCH_ENTRY
        if msg.width not in (2, 4):
            return p.ArmRefusal.BAD_WIDTH
        try:
            nibe.validate_reply(msg.frame)
        except nibe.FrameError:
            return p.ArmRefusal.INVALID_FRAME
        if msg.frame[1] != nibe.WRITE_TOKEN or msg.frame[2] != 6:
            return p.ArmRefusal.INVALID_FRAME
        if int.from_bytes(msg.frame[3:5], "little") != msg.register:
            return p.ArmRefusal.REGISTER_MISMATCH
        return None

    # --- firing --------------------------------------------------------------------------

    def tick(self, now_us: int) -> None:
        if not self._firing and self.armed and now_us - self._heard_us >= self.silence_us:
            log.warning(
                "dead-man: silent for %d s, putting %d back",
                self.silence_us // 10**6,
                len(self.armed),
            )
            self._firing = True
            self._queue = [self.armed[n] for n in sorted(self.armed)]
        if self._firing and self._current is None:
            self._next(now_us)
        if self._dirty and now_us - self._saved_us >= SAVE_EVERY_US:
            self.save(now_us)

    def on_message(self, message: p.Fate | p.Answer, now_us: int) -> None:
        """A fate or an answer for one of its own requests."""
        if self._pending is None or message.id != self._pending[0] or self._current is None:
            return
        entry, step = self._current, self._pending[1]
        if isinstance(message, p.Fate):
            if message.stage in (p.Stage.DROPPED, p.Stage.PUMP_NAK):
                self._done(Outcome(entry.number, p.FiredOutcome.NO_ANSWER, entry.register), now_us)
            return
        if message.status != p.AnswerStatus.OK:
            self._done(Outcome(entry.number, p.FiredOutcome.NO_ANSWER, entry.register), now_us)
            return
        try:
            payload = nibe.parse_telegram(message.frame).payload
        except nibe.FrameError:
            self._done(Outcome(entry.number, p.FiredOutcome.NO_ANSWER, entry.register), now_us)
            return
        if step == "read":
            value = _value(payload, entry.width)
            if value is None or int.from_bytes(payload[:2], "little") != entry.register:
                self._done(Outcome(entry.number, p.FiredOutcome.NO_ANSWER, entry.register), now_us)
            elif value == _restores(entry):
                # Already back: a firing before a restart wrote it, or someone put it back.
                self._done(
                    Outcome(entry.number, p.FiredOutcome.PUT_BACK, entry.register, value), now_us
                )
            elif value != entry.expected & _mask(entry.width):
                # Someone changed it while the client was silent: theirs stands.
                self._done(
                    Outcome(entry.number, p.FiredOutcome.SKIPPED, entry.register, value), now_us
                )
            else:
                self._request(nibe.WRITE_TOKEN, entry.frame, "write", now_us)
        else:
            taken = bool(payload) and payload[0] == 1
            outcome = p.FiredOutcome.PUT_BACK if taken else p.FiredOutcome.REFUSED
            self._done(Outcome(entry.number, outcome, entry.register, entry.expected), now_us)

    def _next(self, now_us: int) -> None:
        if not self._queue:
            self._firing = False
            return
        entry = self._queue.pop(0)
        if self.armed.get(entry.number) != entry:
            return  # disarmed or armed anew since the firing began: the client knows best
        del self.armed[entry.number]
        self._current = entry
        self._dirty = True
        self._request(nibe.READ_TOKEN, nibe.read_request(entry.register), "read", now_us)

    def _request(self, token: int, frame: bytes, step: str, now_us: int) -> None:
        self._next_id += 1
        self._pending = (self._next_id, step)
        request = p.Request(
            address=nibe.MODBUS40,
            token=token,
            flags=p.RequestFlag.EXPECT_ANSWER,
            ttl_ms=REQUEST_TTL_MS,
            frame=frame,
            id=self._next_id,
        )
        self._submit(self, request, now_us)

    def _done(self, outcome: Outcome, now_us: int) -> None:
        log.warning(
            "dead-man: entry %d (%d): %s", outcome.entry, outcome.register, outcome.outcome.name
        )
        self.fired[outcome.entry] = Outcome(
            outcome.entry, outcome.outcome, outcome.register, outcome.read, now_us
        )
        self._current = None
        self._pending = None
        self._dirty = True
        self.save(now_us)  # a firing is kept at once: a reboot must not fire it again
        if self._firing:
            self._next(now_us)

    # --- the state file ------------------------------------------------------------------

    def save(self, now_us: int) -> None:
        self._saved_us = now_us
        self._dirty = False
        if self._state is None:
            return
        # The entry being fired is still to be put back until its outcome is kept: a restart
        # fires it again, and its read then shows whether this firing already did.
        armed = dict(self.armed)
        if self._current is not None:
            armed.setdefault(self._current.number, self._current)
        body = {
            "silence_s": self.silence_us // 1_000_000,
            "armed": [{**asdict(e), "frame": e.frame.hex()} for e in armed.values()],
            "fired": [
                {**asdict(o), "outcome": int(o.outcome), "fired_us": 0} for o in self.fired.values()
            ],
        }
        tmp = self._state.with_suffix(".tmp")
        try:
            tmp.write_text(json.dumps(body))
            os.replace(tmp, self._state)
        except OSError as e:
            log.error("dead-man: the state file %s couldn't be written: %s", self._state, e)

    def _load(self, path: Path) -> None:
        with contextlib.suppress(FileNotFoundError):
            try:
                body = json.loads(path.read_text())
                self.silence_us = int(body.get("silence_s", SILENCE_S[1])) * 1_000_000
                self.armed = {
                    int(e["number"]): Entry(
                        int(e["number"]),
                        int(e["width"]),
                        int(e["register"]),
                        int(e["expected"]),
                        bytes.fromhex(e["frame"]),
                    )
                    for e in body.get("armed", ())
                }
                self.fired = {
                    int(o["entry"]): Outcome(
                        int(o["entry"]),
                        p.FiredOutcome(int(o["outcome"])),
                        int(o["register"]),
                        int(o["read"]),
                    )
                    for o in body.get("fired", ())
                }
                log.info("dead-man: %d entries armed from %s", len(self.armed), path)
            except (OSError, ValueError, KeyError, TypeError) as e:
                log.error("dead-man: the state file %s couldn't be read: %s", path, e)


def _mask(width: int) -> int:
    return (1 << (8 * width)) - 1


def _restores(entry: Entry) -> int:
    """The value the entry's write puts back, at its width: the write's four value bytes."""
    return int.from_bytes(entry.frame[5:9], "little") & _mask(entry.width)


def _value(payload: bytes, width: int) -> int | None:
    """The register's value in a 0x6A answer: two 16-bit words after the register, the
    first the low half."""
    if len(payload) < 2 + width:
        return None
    return int.from_bytes(payload[2 : 2 + width], "little")
