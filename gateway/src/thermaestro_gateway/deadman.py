"""The dead-man: put back what a silent client changed (docs/gateway-protocol.md §15).

An authenticated client arms entries: a register, the value it wrote, and the write that
puts the register's earlier value back. Each client is known by the id it gives in HELLO,
the same across its sessions, and has its own entries, its own silence and its own
reports. Once no session of a client has said anything for its silence, each of its armed
entries is fired in turn: the gateway reads the register, and only where it still holds
what the client wrote does it send the write. Someone who changed it meanwhile is never
written over. Each entry fires once; what came of it is kept until that client
acknowledges it.

It is a client of the engine like any other: its reads and writes wait in the same
queues, and a write waits for the one in flight. There is no I/O here but the state file:
an armed entry is saved there before it is confirmed, so a reboot of the gateway keeps it.
"""

import contextlib
import json
import logging
import os
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path

from thermaestro_gateway import nibe
from thermaestro_gateway import protocol as p

log = logging.getLogger(__name__)

ENTRIES = 16
MAX_CLIENTS = 4
"""Clients holding entries or reports at once."""
SILENCE_S = (300, 900, 7200)  # minimum, default, maximum
SAVE_EVERY_US = 600_000_000
"""How often a disarm or an acknowledgment is saved, at most."""
REPORT_KEPT_US = 30 * 24 * 3600 * 1_000_000
"""A report no session of its client acknowledges is dropped after this long."""
REQUEST_TTL_MS = 60_000
FIRST_ID = 0x8000_0000
"""Its own request ids, apart from any client's (they never share an outbox)."""
UNOWNED = b""
"""Entries saved by a gateway of revision 4, which knew no clients: they go to the first
client granted the dead-man."""


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
    kept_us: int = 0
    """When it was kept, on this boot's clock: a reboot counts its age from the boot."""


@dataclass(slots=True)
class Client:
    silence_us: int
    heard_us: int
    armed: dict[int, Entry] = field(default_factory=dict)
    fired: dict[int, Outcome] = field(default_factory=dict)

    @property
    def holds(self) -> bool:
        return bool(self.armed or self.fired)


Submit = Callable[[object, p.Request, int], None]


class DeadMan:
    def __init__(self, submit: Submit, now_us: int, *, state: Path | None = None) -> None:
        self._submit = submit
        self.clients: dict[bytes, Client] = {}
        self._firing: set[bytes] = set()
        self._queue: list[tuple[bytes, Entry]] = []
        self._current: tuple[bytes, Entry] | None = None
        self._pending: tuple[int, str] | None = None
        """The request in flight for the current entry: its id, and "read" or "write"."""
        self._just_fired: list[tuple[bytes, Outcome]] = []
        self._next_id = FIRST_ID
        self._state = state
        self._dirty = False
        self._saved_us = now_us
        if state is not None:
            self._load(state, now_us)

    # --- the client ----------------------------------------------------------------------

    def grant(
        self, client: bytes, asked_s: int, asked_entries: int, now_us: int
    ) -> tuple[int, int] | None:
        """The silence and entries granted to a session of `client`; None when the gateway
        already holds entries for as many other clients as it can."""
        low, _, high = SILENCE_S
        seconds = min(max(asked_s, low), high)
        known = self.clients.get(client)
        if known is None:
            unowned = self.clients.get(UNOWNED)
            if unowned is not None:
                log.info("dead-man: the entries kept before clients were known are this one's")
                known = self.clients[client] = self.clients.pop(UNOWNED)
                if UNOWNED in self._firing:
                    self._firing.discard(UNOWNED)
                    self._firing.add(client)
                self._queue = [(client if c == UNOWNED else c, e) for c, e in self._queue]
                if self._current is not None and self._current[0] == UNOWNED:
                    self._current = (client, self._current[1])
                # Kept as theirs: a reboot must not give them to whoever comes first then.
                self._dirty = True
            elif self._holding(client) >= MAX_CLIENTS:
                log.warning("dead-man: entries are held for %d other clients", MAX_CLIENTS)
                return None
            else:
                known = self.clients[client] = Client(seconds * 1_000_000, now_us)
        if seconds * 1_000_000 != known.silence_us:
            known.silence_us = seconds * 1_000_000
            self._dirty = True
        return seconds, min(max(asked_entries, 0), ENTRIES)

    def counts(self, client: bytes) -> tuple[int, int]:
        """What the gateway holds for `client`: entries armed, and reports not acknowledged."""
        known = self.clients.get(client)
        return (len(known.armed), len(known.fired)) if known is not None else (0, 0)

    def heard(self, client: bytes, now_us: int) -> None:
        """A session of `client` spoke: its silence starts again, and a firing of its
        entries under way stops after the entry being fired."""
        known = self.clients.get(client)
        if known is None:
            return
        known.heard_us = now_us
        if client in self._firing:
            left = [e for c, e in self._queue if c == client]
            log.info("dead-man: the client is back, %d entries left armed", len(left))
            self._firing.discard(client)
            self._queue = [(c, e) for c, e in self._queue if c != client]

    def arm(self, client: bytes, msg: p.Arm, entries: int, silence_s: int, now_us: int) -> p.Armed:
        """`entries` and `silence_s` as granted to the session."""
        refusal = self._refusal(msg, entries) or self._fired(client, msg.entry)
        known = self.clients.get(client)
        if (
            refusal is None
            and (known is None or not known.holds)
            and self._holding(client) >= MAX_CLIENTS
        ):
            refusal = p.ArmRefusal.NO_ROOM
        if refusal is not None:
            return p.Armed(entry=msg.entry, result=p.ArmResult.REFUSED, detail=refusal, id=msg.id)
        if known is None:  # it held nothing, and was forgotten while its session lasted
            known = self.clients[client] = Client(silence_s * 1_000_000, now_us)
        before = known.armed.get(msg.entry)
        known.armed[msg.entry] = Entry(msg.entry, msg.width, msg.register, msg.expected, msg.frame)
        # Saved before it is confirmed: a client told "armed" may stop, and a reboot of the
        # gateway after that must still fire it.
        if not self.save(now_us):
            if before is None:
                del known.armed[msg.entry]
            else:
                known.armed[msg.entry] = before
            self._dirty = True
            return p.Armed(
                entry=msg.entry,
                result=p.ArmResult.REFUSED,
                detail=p.ArmRefusal.NOT_KEPT,
                id=msg.id,
            )
        return p.Armed(entry=msg.entry, result=p.ArmResult.ARMED, id=msg.id)

    def disarm(self, client: bytes, entry: int, msg_id: int) -> p.Armed:
        refusal = self._fired(client, entry)
        if refusal is not None:
            return p.Armed(entry=entry, result=p.ArmResult.REFUSED, detail=refusal, id=msg_id)
        known = self.clients.get(client)
        if known is not None and known.armed.pop(entry, None) is not None:
            self._dirty = True
        return p.Armed(entry=entry, result=p.ArmResult.DISARMED, id=msg_id)

    def _fired(self, client: bytes, entry: int) -> p.ArmRefusal | None:
        """An entry being fired, or whose outcome isn't acknowledged, keeps its number: a
        report for it is then always for what the client armed last."""
        known = self.clients.get(client)
        if known is not None and entry in known.fired:
            return p.ArmRefusal.FIRED
        current = self._current
        if current is not None and current[0] == client and current[1].number == entry:
            return p.ArmRefusal.FIRED
        return None

    def ack_fired(self, client: bytes, entry: int, msg_id: int) -> p.Armed:
        known = self.clients.get(client)
        if known is not None and known.fired.pop(entry, None) is not None:
            self._dirty = True
        return p.Armed(entry=entry, result=p.ArmResult.DISARMED, id=msg_id)

    def stop(self, now_us: int) -> None:
        """The gateway is stopping: an entry being fired is armed again, and what is armed
        is kept, so a restart fires it."""
        if self._current is not None:
            client, entry = self._current
            known = self.clients.get(client)
            if known is not None:
                known.armed.setdefault(entry.number, entry)
        self._current = None
        self._pending = None
        self._firing = set()
        self._queue = []
        self.save(now_us)

    def reports(self, client: bytes, now_us: int) -> list[p.Fired]:
        known = self.clients.get(client)
        if known is None:
            return []
        return [_report(o, now_us) for o in sorted(known.fired.values(), key=lambda o: o.entry)]

    def just_fired(self, now_us: int) -> list[tuple[bytes, p.Fired]]:
        """The entries that fired since this was last asked, each with its client: told to
        that client's sessions at once, as one may be back while an entry was under way."""
        done, self._just_fired = self._just_fired, []
        return [(client, _report(o, now_us)) for client, o in done]

    def _holding(self, client: bytes) -> int:
        """How many other clients the gateway holds entries or reports for."""
        return sum(1 for c, known in self.clients.items() if c != client and known.holds)

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
        for client in sorted(self.clients):
            known = self.clients[client]
            if (
                client not in self._firing
                and known.armed
                and now_us - known.heard_us >= known.silence_us
            ):
                log.warning(
                    "dead-man: a client silent for %d s, putting %d back",
                    known.silence_us // 10**6,
                    len(known.armed),
                )
                self._firing.add(client)
                self._queue += [(client, known.armed[n]) for n in sorted(known.armed)]
        if self._current is None:
            self._next(now_us)
        self._forget(now_us)
        if self._dirty and now_us - self._saved_us >= SAVE_EVERY_US:
            self.save(now_us)

    def _forget(self, now_us: int) -> None:
        """Drop the reports nobody acknowledged in time, then the clients holding nothing
        whose sessions have gone quiet."""
        for known in self.clients.values():
            for entry, o in list(known.fired.items()):
                if now_us - o.kept_us >= REPORT_KEPT_US:
                    log.warning("dead-man: entry %d's report was never acknowledged", entry)
                    del known.fired[entry]
                    self._dirty = True
        busy = {c for c, _ in self._queue} | self._firing
        if self._current is not None:
            busy.add(self._current[0])
        for client in [
            c
            for c, known in self.clients.items()
            if not known.holds and c not in busy and now_us - known.heard_us >= known.silence_us
        ]:
            del self.clients[client]

    def on_message(self, message: p.Fate | p.Answer, now_us: int) -> None:
        """A fate or an answer for one of its own requests."""
        if self._pending is None or message.id != self._pending[0] or self._current is None:
            return
        entry, step = self._current[1], self._pending[1]
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
        while self._queue:
            client, entry = self._queue.pop(0)
            known = self.clients.get(client)
            if not any(c == client for c, _ in self._queue):
                self._firing.discard(client)  # its last entry: once done, it isn't firing
            if known is None or known.armed.get(entry.number) != entry:
                continue  # disarmed or armed anew since the firing began: the client knows best
            del known.armed[entry.number]
            self._current = (client, entry)
            self._dirty = True
            self._request(nibe.READ_TOKEN, nibe.read_request(entry.register), "read", now_us)
            return

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
        if self._current is None:
            return
        client = self._current[0]
        log.warning(
            "dead-man: entry %d (%d): %s", outcome.entry, outcome.register, outcome.outcome.name
        )
        kept = Outcome(
            outcome.entry, outcome.outcome, outcome.register, outcome.read, now_us, now_us
        )
        known = self.clients.get(client)
        if known is not None:
            known.fired[outcome.entry] = kept
        self._just_fired.append((client, kept))
        self._current = None
        self._pending = None
        self._dirty = True
        self.save(now_us)  # a firing is kept at once: a reboot must not fire it again
        self._next(now_us)

    # --- the state file ------------------------------------------------------------------

    def save(self, now_us: int) -> bool:
        """Write what is held to the state file, and through to the disk. False if it
        couldn't be; with no state file, nothing is kept, and that is no failure."""
        self._saved_us = now_us
        self._dirty = False
        if self._state is None:
            return True
        body = {
            "clients": [
                {
                    "id": client.hex(),
                    "silence_s": known.silence_us // 1_000_000,
                    "armed": [
                        {**asdict(e), "frame": e.frame.hex()} for e in self._kept(client, known)
                    ],
                    "fired": [
                        {
                            "entry": o.entry,
                            "outcome": int(o.outcome),
                            "register": o.register,
                            "read": o.read,
                        }
                        for o in known.fired.values()
                    ],
                }
                for client, known in self.clients.items()
                if known.holds or (self._current is not None and self._current[0] == client)
            ],
        }
        tmp = self._state.with_suffix(".tmp")
        try:
            with tmp.open("w") as f:
                f.write(json.dumps(body))
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self._state)
            directory = os.open(self._state.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        except OSError as e:
            log.error("dead-man: the state file %s couldn't be written: %s", self._state, e)
            self._dirty = True
            return False
        return True

    def _kept(self, client: bytes, known: Client) -> list[Entry]:
        """Its armed entries, and the one being fired: that is still to be put back until
        its outcome is kept, so a restart fires it again, and its read then shows whether
        this firing already did."""
        armed = dict(known.armed)
        if self._current is not None and self._current[0] == client:
            armed.setdefault(self._current[1].number, self._current[1])
        return list(armed.values())

    def _load(self, path: Path, now_us: int) -> None:
        with contextlib.suppress(FileNotFoundError):
            try:
                body = json.loads(path.read_text())
                if "clients" in body:
                    saved = [
                        (bytes.fromhex(c["id"]), c) for c in body["clients"] if isinstance(c, dict)
                    ]
                else:  # revision 4: one set of entries, for whoever comes first
                    saved = [(UNOWNED, body)]
                for client, c in saved:
                    known = Client(int(c.get("silence_s", SILENCE_S[1])) * 1_000_000, now_us)
                    known.armed = {
                        int(e["number"]): Entry(
                            int(e["number"]),
                            int(e["width"]),
                            int(e["register"]),
                            int(e["expected"]),
                            bytes.fromhex(e["frame"]),
                        )
                        for e in c.get("armed", ())
                    }
                    known.fired = {
                        int(o["entry"]): Outcome(
                            int(o["entry"]),
                            p.FiredOutcome(int(o["outcome"])),
                            int(o["register"]),
                            int(o["read"]),
                            0,
                            now_us,
                        )
                        for o in c.get("fired", ())
                    }
                    if known.holds:
                        self.clients[client] = known
                        self._dirty = self._dirty or client == UNOWNED  # saved in today's form
                log.info(
                    "dead-man: %d entries armed for %d clients from %s",
                    sum(len(k.armed) for k in self.clients.values()),
                    len(self.clients),
                    path,
                )
            except (OSError, ValueError, KeyError, TypeError, AttributeError) as e:
                self.clients = {}
                self._dirty = False
                log.error("dead-man: the state file %s couldn't be read: %s", path, e)


def _report(o: Outcome, now_us: int) -> p.Fired:
    return p.Fired(
        entry=o.entry,
        outcome=o.outcome,
        register=o.register,
        read=o.read,
        fired_us=o.fired_us,
        gw_time_us=now_us,
    )


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
