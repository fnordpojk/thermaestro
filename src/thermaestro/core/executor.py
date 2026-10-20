"""The write path: the one way anything is changed on a device.

Every change goes through `Executor.act`. The lever is taken over (claimed) first, with
the value it was found at kept as its baseline; the request is checked, sent and read
back; and what became of it is recorded. In shadow the same checks run and the request is
recorded, but nothing is sent, so what shadow shows is what control would have done.

`restore` puts every lever back as it was found: on the way out, after an input the
decisions rest on goes stale, and when the planner stops answering. A change Thermaestro
didn't make lets go of the lever: it is reported, and never written over.

A change is kept as in flight before it is sent, so a process that stops before its fate is
known still knows the device may have taken it: the next start reads the device, and puts
it back. A change whose fate stays unknown (the connection ended after it was sent, no
answer came in time, or it couldn't be read back) stays in flight until a fresh reading
says: the new value, taken; the value from before, it didn't arrive; anything else,
someone else changed it, which lets go of the lever. Where the lever can't be read, it is
taken as the state from which putting back is still needed. Nothing is decided on such a
lever from a value read before: acting on it, putting it back included, first reads the
device anew, and waits while it can't be.

A lever is put back only once it is as found: a setting read back so, a hold released as
the device accepted. One that isn't (its plugin down, the device refusing, an answer that
didn't come) is owed: in whatever mode it is now, and with the planner stopped too, it is
tried again, at once when its plugin comes back, until it is put back or someone changes
it, which lets go of it. Meanwhile it is an event, and it isn't decided on in shadow.

The device's identity (vendor, model, firmware, serial, as its plugin gives them) is kept
when a lever is taken over. Should the device be another when the lever is put back, the
lever is let go rather than given a value found on a different device.

A hold's release is the plugin's to know (what an emulated block puts back), and a plugin
keeps it across its own restarts. When a plugin describes an engaged hold anew, what it
acts on has changed (the hot-water block follows the mode): it is released and engaged
again.

A lever the vocabulary keeps for people (an alarm reset) is never used by the planner,
the core or an MQTT request.

`write` is a person's change of one of the device's own settings, outside the levers: never
the planner's, nor one a lever in control has taken over. Whatever Thermaestro kept of a
lever over the same setting is dropped after it, so the lever is taken over afresh, from the
new value. A setting lever's baseline can also be set by a person (`set_baseline`).
"""

import asyncio
import contextlib
import json
import logging
import re
from collections.abc import Callable, Collection, Coroutine
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from .. import clock as clocks
from ..cap import Closed, Message, vocabulary
from ..cap.client import CapError, Link
from ..cap.defaults import assume
from ..cap.messages import Described, ForeignWrite, Op
from ..cap.model import Envelope, Lever, Param, Value
from ..store import Control, Database, LeverMode, Transaction
from .audit import AuditLog
from .events import PUT_BACK, Events, Raise, mark
from .host import PluginHost, State
from .values import Key, Values

log = logging.getLogger(__name__)

DAY_S = 86_400.0
WATCHDOG_S = 300.0
"""A planner that hasn't checked in for this long has stopped: what it set is put back."""
VERIFY_S = 30.0
"""How long a readback waits for the value to show, where the lever names no delay."""
ACT_TIMEOUT_S = 60.0
"""The longest wait for the plugin's next word on a request. A Nibe gateway write waits
up to 30 s for its turn on the bus."""
KEEP_ACTS_DAYS = 400
HOLD_SLACK_S = 60.0
"""A change asked at the next slot may come a little short of the hold, by however long
the last write took on its way: the hold is one change per slot, not to the second."""
RETRY_S = 10.0
"""How long a lever that couldn't be put back waits to be tried again; each try that fails
doubles it, up to the watchdog's period. Its plugin coming back tries it at once."""
RETRY_DOUBLINGS = 16
"""Doubled no more often than this: far past the watchdog's period, and finite."""

NOT_BACK = mark("%(lever)s couldn't be put back as found (%(why)s): it is tried again")

Outcome = Literal[
    "verified",
    "not_kept",
    "unverifiable",
    "awaiting_effect",
    "timeout",
    "lost",
    "shadowed",
    "dropped",
    "device_refused",
    "refused",
    "unchanged",
    "replaced",
]

COUNTED: frozenset[str] = frozenset(
    {
        "verified",
        "not_kept",
        "unverifiable",
        "awaiting_effect",
        "timeout",
        "lost",
        "shadowed",
        "device_refused",
    }
)
"""Requests that reached the device, or in shadow would have: what the budget and the
guard count. Shadow counts too, so it decides as control would."""

TAKEN: frozenset[str] = frozenset(
    {"verified", "unverifiable", "awaiting_effect", "timeout", "lost"}
)
"""Outcomes after which the device may hold the new value, so a restore must undo it."""
GONE = "the plugin's connection ended before it could be read back"
LOST = "the plugin's connection ended before it said what became of it"
UNSETTLED = "what became of its last change isn't known yet"

AUTOMATIC = frozenset({"planner", "core", "mqtt"})
"""Principals that aren't a person acting: a lever kept for people is refused to them."""

OPS: dict[str, frozenset[str]] = {
    "setting": frozenset({"set"}),
    "hold": frozenset({"engage", "release"}),
    "trigger": frozenset({"fire", "cancel"}),
    "feed": frozenset({"feed"}),
}

_CONDITION = re.compile(r"^\s*(\S+)\s*(==|!=)\s*(.+?)\s*$")


@dataclass(frozen=True, slots=True)
class Result:
    outcome: Outcome
    detail: str | None = None


@dataclass
class Claim:
    """A lever Thermaestro has taken over, and what it knows of its state."""

    lever: str
    """`<instance>:<path>`."""
    claimed: float
    baseline: Value | None
    """The setting's value when it was first taken over; None for a hold (released)."""
    mode: LeverMode
    held: bool = False
    """A hold engaged, or in shadow, one that would be."""
    last: Value | None = None
    """What Thermaestro last set, or in shadow would have; None when it is as found."""
    last_t: float | None = None
    drift: str | None = None
    """Why the lever was let go: a change Thermaestro didn't make. It stays let go until
    the household decides."""
    pending: dict[str, Any] | None = None
    """A change sent whose fate isn't known yet, its `op` and `params`: until it is, the
    device may have taken it."""
    unit: dict[str, str] | None = None
    """The identity of the device it was taken over on, where its plugin gives one."""

    @property
    def changed(self) -> bool:
        """Whether the device may differ from how it was found because of Thermaestro."""
        return (
            self.held
            or self.pending is not None
            or (self.last is not None and not same(self.last, self.baseline))
        )


@dataclass
class _Owed:
    """A lever that couldn't be put back, and when it is tried again."""

    why: str
    tries: int = 0
    due: float = 0.0


def split(ref: str) -> tuple[str, str]:
    instance, _, path = ref.partition(":")
    return instance, path


class Executor:
    def __init__(
        self,
        db: Database,
        host: PluginHost,
        values: Values,
        audit: AuditLog,
        *,
        clock: Callable[[], float] = clocks.time,
        verify_s: float = VERIFY_S,
        act_timeout_s: float = ACT_TIMEOUT_S,
        watchdog_s: float = WATCHDOG_S,
        poll_s: float = 1.0,
    ) -> None:
        self._db = db
        self._host = host
        self._values = values
        self._audit = audit
        self._clock = clock
        self._verify_s = verify_s
        self._act_timeout_s = act_timeout_s
        self._watchdog_s = watchdog_s
        self.events: Events | None = None
        """Where a restore the watchdog made is told, once the core has its events."""
        self._poll_s = poll_s
        self.claims: dict[str, Claim] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._generation: dict[str, int] = {}
        self._writing: set[str] = set()
        self._beat: float | None = None
        self.checked_in: float | None = None
        """When the planner last checked in."""
        self._renewed: dict[str, float] = {}
        self._owed: dict[str, _Owed] = {}
        self._unsettled: dict[str, float] = {}
        self._born = self._clock()
        """A change in flight from an earlier run is settled by any reading since this."""
        """Changes whose fate isn't known, by when that was found: a reading after it says."""
        self._tasks: set[asyncio.Task[None]] = set()

    # --- Running ------------------------------------------------------------------------

    async def start(self) -> None:
        self.claims = await self._db.run(_load_claims)
        await self._db.run(lambda t: _prune(t, self._clock() - KEEP_ACTS_DAYS * DAY_S))
        self._values.listeners.append(self._on_value)
        self._host.listeners.append(self._on_event)
        self._host.on_up.append(self._on_up)
        for ref, claim in self.claims.items():
            if claim.mode == "control" and claim.changed and claim.drift is None:
                self._spawn(self._reconcile(ref))
        self._spawn(self._watch())
        self._spawn(self._leases())
        self._spawn(self._retry())

    async def stop(self) -> None:
        with contextlib.suppress(ValueError):
            self._values.listeners.remove(self._on_value)
        with contextlib.suppress(ValueError):
            self._host.listeners.remove(self._on_event)
        with contextlib.suppress(ValueError):
            self._host.on_up.remove(self._on_up)
        for task in list(self._tasks):
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)

    def heartbeat(self) -> None:
        """The planner checks in after each round. From the first, a planner silent for
        `watchdog_s` has everything put back."""
        self._beat = self.checked_in = self._clock()

    def readiness(self) -> dict[str, Any]:
        """How the write path stands: whether the planner checks in (`not yet`, `checking
        in`, or `silent` for over the watchdog's period), how many levers couldn't be put
        back, and how many changes are in flight, their fate not known yet."""
        if self.checked_in is None:
            planner = "not yet"
        elif self._clock() - self.checked_in > self._watchdog_s:
            planner = "silent"
        else:
            planner = "checking in"
        return {
            "planner": planner,
            "not_put_back": len(self._owed),
            "in_flight": sum(1 for c in self.claims.values() if c.pending is not None),
        }

    # --- Asking for a change ------------------------------------------------------------

    def lever(self, ref: str) -> Lever | None:
        instance, path = split(ref)
        found = self._host.instances.get(instance)
        if found is None or found.described is None:
            return None
        return next((lv for lv in found.described.levers if lv.path == path), None)

    async def act(
        self,
        ref: str,
        op: Op,
        params: dict[str, Value] | None = None,
        *,
        who: str,
        why: str | None = None,
    ) -> Result:
        """Change a lever, `<instance>:<path>`, through every step of the write path. A
        newer request for the same lever replaces one still waiting."""
        generation = self._generation[ref] = self._generation.get(ref, 0) + 1
        async with self._lock(ref):
            if self._generation[ref] != generation:
                return Result("replaced", "a newer request for this lever came")
            return await self._act(ref, op, dict(params or {}), who, why)

    async def write(
        self, instance: str, point: str, value: float, *, who: str, why: str | None = None
    ) -> Result:
        """A person's change of one of the device's own settings (`point`, under `x.<plugin>`):
        sent, read back and recorded like a lever's."""
        ref = f"{instance}:{point}"
        async with self._lock(ref):
            return await self._write(ref, instance, point, value, who, why)

    async def set_baseline(self, ref: str, value: Value, *, who: str) -> str | None:
        """Set what a setting lever is put back to: a value a person chooses rather than
        the one it was found at. Why not, or None."""
        lever = self.lever(ref)
        if lever is None:
            return "no such lever, or its plugin isn't running"
        if lever.kind != "setting":
            return "only a setting has a baseline"
        async with self._lock(ref):
            claim = self.claims.get(ref)
            if claim is not None and claim.drift is not None:
                return f"let go after a change Thermaestro didn't make: {claim.drift}"
            refusal = self._fits(lever, {"value": value}, claim)
            if refusal is not None:
                return refusal
            before = claim.baseline if claim is not None else None
            if claim is None:
                mode = (await self._control()).levers.get(ref, "off")
                claim = Claim(ref, self._clock(), value, mode)
                self.claims[ref] = claim
            claim.baseline = value
            if claim.last is not None and same(claim.last, value, lever.params.get("value")):
                claim.last = None
            await self._save(claim)
        await self._audit.record(
            who, "lever.baseline", details={"lever": ref, "from": before, "to": value}
        )
        return None

    async def budget(self, instance: str) -> tuple[int, int]:
        """Writes in the last day for an instance's levers, and the soft budget: the
        planner aims to stay under it."""
        control = await self._control()
        since = self._clock() - DAY_S
        used = await self._db.run(lambda t: _count_instance(t, instance, since))
        return used, control.soft_budget

    async def set_mode(self, ref: str, mode: LeverMode, *, who: str) -> None:
        """Put a lever off, in shadow or in control. Leaving control puts it back as it
        was found; one that can't be yet stays as it is, owed, until it is."""
        control = await self._control()
        before = control.levers.get(ref, "off")
        levers = {k: v for k, v in control.levers.items() if k != ref}
        if mode != "off":
            levers[ref] = mode
        await self._db.put(control.model_copy(update={"levers": levers}))
        await self._audit.record(
            who, "lever.mode", details={"lever": ref, "from": before, "to": mode}
        )
        if before == "control" and mode != "control":
            await self.restore(f"{who} took the lever out of control", [ref])
        claim = self.claims.get(ref)
        if claim is not None and claim.mode != mode:
            if _owing(claim):
                return
            claim.mode = mode
            claim.held, claim.last, claim.last_t = False, None, None
            await self._save(claim)

    async def confirm_off(self, ref: str, features: Collection[str], *, who: str) -> None:
        """The household says these competing features of a lever are switched off."""
        control = await self._control()
        confirmed = dict(control.confirmed_off)
        confirmed[ref] = tuple(sorted(features))
        await self._db.put(control.model_copy(update={"confirmed_off": confirmed}))
        await self._audit.record(
            who, "lever.confirm_off", details={"lever": ref, "features": sorted(features)}
        )

    async def accept_drift(self, ref: str, *, who: str) -> None:
        """The household keeps a change Thermaestro didn't make: the lever is let go for
        good, and taken over again from how it is now."""
        claim = self.claims.pop(ref, None)
        if claim is None:
            return
        await self._db.run(lambda t: _drop_claims(t, [ref]))
        await self._audit.record(who, "lever.drift_accepted", details={"lever": ref})

    async def forget(self, refs: Collection[str] | None = None) -> None:
        """Drop what is kept of levers that are as they were found, or were let go, so the
        next request takes them over afresh, with a new baseline: the rig, between runs."""
        gone = [
            ref
            for ref, claim in self.claims.items()
            if (refs is None or ref in refs) and not (claim.changed and claim.drift is None)
        ]
        for ref in gone:
            del self.claims[ref]
        await self._db.run(lambda t: _drop_claims(t, gone))

    async def restore(self, why: str, refs: Collection[str] | None = None) -> dict[str, Result]:
        """Put levers back as they were found: release holds, set settings to their
        baselines. Nothing a person changed meanwhile is written over. One that can't be
        put back is owed, and tried again until it is."""
        results: dict[str, Result] = {}
        for ref, claim in list(self.claims.items()):
            if refs is not None and ref not in refs:
                continue
            if claim.drift is not None or not claim.changed:
                continue
            if claim.mode != "control":
                claim.held, claim.last, claim.last_t = False, None, None
                await self._save(claim)
                continue
            async with self._lock(ref):
                other = self._other_device(claim)
                if other is not None:
                    await self._let_go(ref, other)
                    results[ref] = Result("refused", other)
                    await self._paid(ref)
                    continue
                try:
                    if claim.held:
                        result = await self._act(ref, "release", {}, "core", why, restoring=True)
                    elif claim.baseline is not None:
                        result = await self._act(
                            ref, "set", {"value": claim.baseline}, "core", why, restoring=True
                        )
                    else:
                        continue
                except Exception as e:
                    # One lever failing never keeps the others from being put back.
                    log.exception("putting %s back failed", ref)
                    result = Result("dropped", f"putting it back failed: {e}")
                if result.outcome == "unchanged":  # already as found
                    claim.held, claim.last, claim.last_t, claim.pending = False, None, None, None
                    await self._save(claim)
                results[ref] = result
            if claim.changed and claim.drift is None:
                await self._owe(ref, why, result.detail or result.outcome)
            else:
                await self._paid(ref)
        if results:
            await self._audit.record(
                "core",
                "lever.restore",
                why=why,
                outcome="ok" if self.put_back(results) else "failed",
                details={ref: r.outcome for ref, r in results.items()},
            )
        return results

    def put_back(self, refs: Collection[str]) -> bool:
        """Whether every one of these levers is as found: none still changed by
        Thermaestro, none let go instead."""
        for ref in refs:
            claim = self.claims.get(ref)
            if claim is not None and (claim.changed or claim.drift is not None):
                return False
        return True

    # --- The steps ----------------------------------------------------------------------

    async def _act(
        self,
        ref: str,
        op: Op,
        params: dict[str, Value],
        who: str,
        why: str | None,
        *,
        restoring: bool = False,
    ) -> Result:
        control = await self._control()
        mode: LeverMode = "control" if restoring else control.levers.get(ref, "off")
        lever = self.lever(ref)
        if mode == "off":
            return Result("refused", "this lever is off")
        if lever is None:
            return await self._record(
                ref,
                op,
                params,
                who,
                why,
                mode,
                Result("dropped", "no such lever, or its plugin isn't running"),
            )
        refusal = self._check(ref, lever, op, params, control, restoring, who)
        claim = self.claims.get(ref)
        if refusal is None and claim is None:
            claim, refusal = await self._claim(ref, lever, mode)
        if refusal is not None or claim is None:
            return await self._record(ref, op, params, who, why, mode, Result("refused", refusal))
        if claim.mode != mode:
            if _owing(claim):  # what it was found at is still to be put back
                refused = Result("refused", "it isn't put back yet")
                return await self._record(ref, op, params, who, why, mode, refused)
            claim.mode = mode
            claim.held, claim.last, claim.last_t = False, None, None
        if claim.pending is not None:
            # Its fate first: a value read before it can't say whether it is back.
            unsettled = await self._settle_now(ref, claim, lever)
            if unsettled is not None:
                return await self._record(
                    ref, op, params, who, why, mode, Result("refused", unsettled)
                )
        if self._unchanged(claim, lever, op, params):
            return Result("unchanged")
        if not restoring:
            since = self._clock() - DAY_S
            written = await self._db.run(lambda t: _count(t, ref, since))
            refusal = self._limits(lever, claim, control, written)
            if refusal is not None:
                return await self._record(
                    ref, op, params, who, why, mode, Result("refused", refusal)
                )
        found = self._found(ref, lever)
        if mode == "control" and claim.unit is None:
            claim.unit = self._unit(ref)
        if mode == "shadow":
            self._took(claim, op, params)
            await self._save(claim)
            return await self._record(
                ref, op, params, who, why, mode, Result("shadowed", "nothing was changed"), found
            )
        # Kept before it is sent: a process that stops before its fate is known still
        # knows the device may have taken it.
        claim.pending = {"op": op, "params": params}
        self._unsettled.pop(ref, None)
        await self._save(claim)
        result = await self._send(ref, lever, op, params)
        unknown = result.outcome in ("lost", "timeout") or (
            result.outcome == "unverifiable" and result.detail == GONE
        )
        if unknown and _readable(lever, op):
            # A reading after now says what became of it (`_on_value`), or the next start.
            self._unsettled[ref] = self._clock()
        else:
            claim.pending = None
            if unknown:
                self._assume(claim, op, params)
            elif result.outcome in TAKEN:
                self._took(claim, op, params)
        await self._save(claim)
        settled = claim.pending is None and result.outcome in TAKEN
        if settled and not restoring and ref in self._owed:
            await self._paid(ref)  # the planner has it again
        return await self._record(ref, op, params, who, why, mode, result, found)

    async def _write(
        self, ref: str, instance: str, point: str, value: float, who: str, why: str | None
    ) -> Result:
        params: dict[str, Value] = {"value": value}

        async def done(result: Result, found: dict[str, dict[str, Any]] | None = None) -> Result:
            return await self._record(
                ref, "write", params, who, why, "control", result, found, action="device.write"
            )

        if who in AUTOMATIC:
            return await done(Result("refused", "only a person may write a device's settings"))
        found_instance = self._host.instances.get(instance)
        link = (
            found_instance.link
            if found_instance is not None and found_instance.state is State.UP
            else None
        )
        if link is None or found_instance is None:
            return await done(Result("dropped", "its plugin isn't running"))
        hello = found_instance.hello
        if hello is None or "write" not in hello.features:
            return await done(Result("refused", "its plugin doesn't take writes"))
        datapoint = point.rpartition("/")[2]
        under: list[str] = []
        for other, claim in self.claims.items():
            lever = self.lever(other)
            if split(other)[0] != instance or lever is None or datapoint not in lever.touches:
                continue
            if claim.drift is not None:  # let go: the household decides that apart
                continue
            if claim.mode == "control":
                return await done(
                    Result(
                        "refused",
                        f"{split(other)[1]} is in control: change its baseline instead",
                    )
                )
            under.append(other)
        control = await self._control()
        written = await self._db.run(lambda t: _count(t, ref, self._clock() - DAY_S))
        if written >= control.guard:
            return await done(
                Result("refused", f"stopped by the runaway guard: {control.guard} writes in a day")
            )
        seen = self._values.latest.get(Key(instance, point))
        found = (
            {point: {"value": seen.value, "unit": seen.unit}}
            if seen is not None and seen.quality == "good"
            else None
        )
        self._writing.add(ref)
        try:
            final = None
            try:
                async for fate in link.write(point, value, timeout=self._act_timeout_s):
                    final = fate
            except TimeoutError:
                return await done(Result("timeout", "the plugin didn't say what became of it"))
            except (Closed, CapError) as e:
                return await done(Result("dropped", f"the plugin's connection: {e}"))
            if final is None or final.stage == "dropped":
                return await done(Result("dropped", final.detail if final else None), found)
            if final.stage == "device_refused":
                return await done(Result("device_refused", final.detail), found)
            result = await self._read_back(link, point, value, final.t)
        finally:
            self._writing.discard(ref)
        if result.outcome in TAKEN:
            # Taken over afresh, from the new value.
            for other in under:
                self.claims.pop(other, None)
            await self._db.run(lambda t: _drop_claims(t, under))
        return await done(result, found)

    async def _read_back(self, link: Link, point: str, value: float, accepted: datetime) -> Result:
        """Whether a written setting reads what was written: its value, or its raw count
        in the steps it reads in (a register shown as words or as on/off)."""
        deadline = clocks.monotonic() + self._verify_s
        seen: Value | None = None
        while True:
            try:
                answer = await link.read([point], after=accepted, timeout=self._verify_s)
            except (TimeoutError, CapError):
                answer = None
            except Closed:
                return Result("unverifiable", GONE)
            if answer is not None:
                for envelope in answer.values:
                    if envelope.quality != "good" or not _after(envelope, accepted):
                        continue
                    raw = envelope.raw
                    step = envelope.resolution or 1.0
                    counted = (
                        isinstance(raw, int | float)
                        and not isinstance(raw, bool)
                        and same(raw * step, value)
                    )
                    if same(envelope.value, value) or counted:
                        return Result("verified")
                    seen = envelope.value
            left = deadline - clocks.monotonic()
            if left <= 0:
                break
            await asyncio.sleep(min(self._poll_s, left))
        if seen is None:
            return Result("timeout", "no fresh value came back")
        return Result("not_kept", f"accepted, but it reads {seen}")

    def found(self, ref: str) -> dict[str, dict[str, Any]]:
        """What the device shows now at the points a lever is checked by and rests on."""
        lever = self.lever(ref)
        return self._found(ref, lever) if lever is not None else {}

    def _found(self, ref: str, lever: Lever) -> dict[str, dict[str, Any]]:
        """What the device shows now where a change of the lever can be compared: the point
        it is checked by, and the ones it writes, each with its unit."""
        instance, path = split(ref)
        node = path.rpartition("/")[0]
        points = [lever.verify.point] if lever.verify.point else []
        points += [f"{node}/{t}" if node else t for t in lever.touches]
        out: dict[str, dict[str, Any]] = {}
        for point in dict.fromkeys(points):
            seen = self._values.latest.get(Key(instance, point))
            if seen is not None and seen.quality == "good":
                out[point] = {"value": seen.value, "unit": seen.unit}
        return out

    def _check(
        self,
        ref: str,
        lever: Lever,
        op: Op,
        params: dict[str, Value],
        control: Control,
        restoring: bool,
        who: str,
    ) -> str | None:
        """Why the request may not go ahead, or None."""
        if lever.unavailable is not None:
            return lever.unavailable
        if op not in OPS[lever.kind]:
            return f"a {lever.kind} lever doesn't take {op}"
        if who in AUTOMATIC and self._for_people(ref, lever):
            return "only a person may use this lever"
        claim = self.claims.get(ref)
        if claim is not None and claim.drift is not None:
            return f"let go after a change Thermaestro didn't make: {claim.drift}"
        if restoring:
            return None
        assumed = assume(lever)
        if not assumed.works:
            return "whether this lever works isn't known well enough to use it unattended"
        for feature in lever.competing_features:
            if feature.can_disable.trusted and feature.can_disable.value is False:
                return f"{feature.name} can't be switched off"
            if feature.name not in control.confirmed_off.get(ref, ()):
                return f"{feature.name} may be on: confirm it's switched off"
        instance, _ = split(ref)
        for other, held in self.claims.items():
            if other == ref or split(other)[0] != instance or held.drift is not None:
                continue
            other_lever = self.lever(other)
            if other_lever is not None and set(other_lever.touches) & set(lever.touches):
                return f"it changes the same as {split(other)[1]}, which is taken over"
        if lever.preconditions.value:
            if not lever.preconditions.trusted:
                return "its preconditions aren't known well enough"
            for condition in lever.preconditions.value:
                if not self._holds(instance, condition):
                    return f"{condition} doesn't hold"
        if op in ("set", "feed"):
            return self._fits(lever, params, claim)
        return None

    def _limits(self, lever: Lever, claim: Claim, control: Control, written: int) -> str | None:
        """The limits on how often a lever changes, checked once a request would change
        something: the runaway guard (`written`: the lever's writes in the last day), and
        the shortest time between two changes."""
        if written >= control.guard:
            return f"stopped by the runaway guard: {control.guard} writes in a day"
        if claim.last_t is None:
            return None
        hold = control.min_hold_s if lever.kind == "setting" else 0.0
        if lever.min_interval_s.trusted:
            hold = max(hold, lever.min_interval_s.value or 0.0)
        if claim.last_t + hold - HOLD_SLACK_S > self._clock():
            return f"changed less than {round(hold / 60)} min ago"
        return None

    def _fits(self, lever: Lever, params: dict[str, Value], claim: Claim | None) -> str | None:
        """Whether the value is one the lever takes: in its range, or, where the range
        isn't known, one already seen on this device."""
        for name, param in lever.params.items():
            if name not in params:
                return f"no {name} given"
            value = params[name]
            seen = [] if claim is None else [claim.baseline, claim.last]
            if param.type == "enum":
                if param.enum.trusted and param.enum.value is not None:
                    if str(value) not in param.enum.value:
                        return f"{value!r} isn't one of {', '.join(param.enum.value)}"
                elif not any(same(value, s) for s in seen):
                    return f"{value!r} hasn't been seen on this device, and its values aren't known"
                continue
            if isinstance(value, bool) or not isinstance(value, int | float):
                return f"{name} is a number"
            limits = assume(lever).ranges.get(name)
            if limits is None:
                if not any(same(value, s) for s in seen):
                    return f"{value} hasn't been seen on this device, and its range isn't known"
                continue
            if limits.min is not None and value < limits.min:
                return f"{value:g} is below {limits.min:g}"
            if limits.max is not None and value > limits.max:
                return f"{value:g} is above {limits.max:g}"
            if (
                limits.step
                and abs(
                    round((value - (limits.min or 0)) / limits.step) * limits.step
                    - (value - (limits.min or 0))
                )
                > 1e-9
            ):
                return f"{value:g} isn't in steps of {limits.step:g}"
        unknown = set(params) - set(lever.params)
        if unknown:
            return f"{', '.join(sorted(unknown))} isn't a parameter of this lever"
        return None

    async def _claim(
        self, ref: str, lever: Lever, mode: LeverMode
    ) -> tuple[Claim | None, str | None]:
        baseline: Value | None = None
        if lever.needs_baseline and lever.kind != "hold":
            found = self._readback(ref, lever)
            if found is None:
                return None, "no baseline: its value can't be read now"
            baseline = reading(found, lever.params.get("value"))
        claim = Claim(ref, self._clock(), baseline, mode)
        self.claims[ref] = claim
        await self._save(claim)
        await self._audit.record(
            "core", "lever.claim", details={"lever": ref, "baseline": baseline, "mode": mode}
        )
        return claim, None

    def _unchanged(self, claim: Claim, lever: Lever, op: Op, params: dict[str, Value]) -> bool:
        if op == "engage":
            return claim.held
        if op == "release":
            return not claim.held
        if op != "set":
            return False
        # What Thermaestro set (in shadow: would have) stands until something else changes
        # it, which lets go of the lever; until it sets anything, the device's own value.
        param = lever.params.get("value")
        target = params.get("value")
        if claim.last is not None:
            return same(claim.last, target, param)
        found = self._readback(claim.lever, lever)
        if found is None:
            return False
        observed = (found.t_observed or found.t_received).timestamp()
        if claim.last_t is not None and observed <= claim.last_t:
            # Read before Thermaestro last set it, back to its baseline: that stands.
            return same(claim.baseline, target, param)
        return same(reading(found, param), target, param)

    async def _send(self, ref: str, lever: Lever, op: Op, params: dict[str, Value]) -> Result:
        instance, path = split(ref)
        found = self._host.instances.get(instance)
        link = found.link if found is not None and found.state is State.UP else None
        if link is None:
            return Result("dropped", "its plugin isn't running")
        self._writing.add(ref)
        try:
            final = None
            try:
                async for fate in link.act(path, op, params, timeout=self._act_timeout_s):
                    final = fate
            except TimeoutError:
                return Result("timeout", "the plugin didn't say what became of it")
            except Closed:
                # Sent, maybe taken: a connection ending says nothing of the device.
                return Result("lost", LOST)
            except CapError as e:
                return Result("dropped", f"the plugin refused it: {e}")
            if final is None or final.stage == "dropped":
                return Result("dropped", final.detail if final else None)
            if final.stage == "device_refused":
                return Result("device_refused", final.detail)
            return await self._verify(link, instance, lever, op, params, final.t)
        finally:
            self._writing.discard(ref)

    async def _verify(
        self,
        link: Link,
        instance: str,
        lever: Lever,
        op: Op,
        params: dict[str, Value],
        accepted: datetime,
    ) -> Result:
        check = lever.verify
        if check.kind == "none":
            return Result("unverifiable", "this lever can't be checked")
        if check.kind == "effect":
            # The expectation is what engaging does; a release only ends it.
            return Result("awaiting_effect", None if op == "release" else check.expectation)
        if op not in ("set", "feed") or check.point is None:
            return Result("unverifiable", "a readback can't show this")
        target = params.get("value")
        param = lever.params.get("value")
        wait = assume(lever).effect_delay_s or self._verify_s
        deadline = clocks.monotonic() + wait
        seen: Value | None = None
        while True:
            try:
                answer = await link.read([check.point], after=accepted, timeout=max(1.0, wait))
            except (TimeoutError, CapError):
                answer = None
            except Closed:
                return Result("unverifiable", GONE)
            if answer is not None:
                for envelope in answer.values:
                    if envelope.quality != "good" or not _after(envelope, accepted):
                        continue
                    if same(reading(envelope, param), target, param):
                        return Result("verified")
                    seen = reading(envelope, param)
            left = deadline - clocks.monotonic()
            if left <= 0:
                break
            await asyncio.sleep(min(self._poll_s, left))
        if seen is None:
            return Result("timeout", "no fresh value came back")
        return Result("not_kept", f"accepted, but it reads {seen}")

    def _took(self, claim: Claim, op: Op, params: dict[str, Value]) -> None:
        if op == "engage":
            claim.held = True
        elif op == "release":
            claim.held = False
        elif op in ("set", "feed"):
            value = params.get("value")
            claim.last = None if same(value, claim.baseline) else value
        claim.last_t = self._clock()
        self._renewed[claim.lever] = claim.last_t

    def _assume(self, claim: Claim, op: Op, params: dict[str, Value]) -> None:
        """A change whose fate isn't known, on a lever that can't be read: taken as the
        state from which putting back is still needed. A release leaves it held; a setting
        set back to its baseline leaves it as it was."""
        if op == "release":
            return
        if op in ("set", "feed") and same(params.get("value"), claim.baseline):
            return
        self._took(claim, op, params)

    def _settle(self, ref: str, claim: Claim, now: Value | None, param: Param | None) -> str | None:
        """A change whose fate wasn't known, settled by a fresh reading; why the lever was
        let go (someone else changed it), or None. Let go at once, so nothing writes over
        that change before it is kept (`_told_let_go`)."""
        assert claim.pending is not None  # noqa: S101 - the caller checks
        sent = claim.pending["params"].get("value")
        before = claim.last if claim.last is not None else claim.baseline
        claim.pending = None
        self._unsettled.pop(ref, None)
        if same(now, sent, param):
            claim.last = None if same(sent, claim.baseline, param) else sent
            claim.last_t = self._clock()
            self._renewed[ref] = claim.last_t
        elif not same(now, before, param):
            claim.drift = f"it reads {now}, neither {sent} as sent nor {before}"
            return claim.drift
        return None

    async def _settle_now(self, ref: str, claim: Claim, lever: Lever) -> str | None:
        """Settle a change in flight before acting on its lever: by reading the device
        anew, since a value read before the change says nothing of it. Why it can't be
        yet (its plugin down, no fresh value), or None."""
        assert claim.pending is not None  # noqa: S101 - the caller checks
        pending = claim.pending
        if not _readable(lever, pending["op"]):
            claim.pending = None
            self._unsettled.pop(ref, None)
            self._assume(claim, pending["op"], pending["params"])
            await self._save(claim)
            return None
        back = await self._read_since(ref, lever, self._unsettled.get(ref, self._born))
        # Meanwhile a reading the device pushed may have settled it, or let go: that stands.
        if self.claims.get(ref) is not claim:
            return UNSETTLED
        if claim.drift is not None:
            return claim.drift
        if claim.pending is not pending:
            return None
        if back is None:
            return UNSETTLED
        param = lever.params.get("value")
        drift = self._settle(ref, claim, reading(back, param), param)
        if drift is not None:
            await self._told_let_go(ref, claim)
            return drift
        await self._save(claim)
        return None

    async def _read_since(self, ref: str, lever: Lever, since: float) -> Envelope | None:
        """A good reading of the lever's point taken after `since`, asked of the device;
        None where there is none (its plugin down, no answer, only older readings)."""
        instance, _ = split(ref)
        found = self._host.instances.get(instance)
        link = found.link if found is not None and found.state is State.UP else None
        point = lever.verify.point
        if link is None or point is None:
            return None
        after = _not_before(since)
        try:
            answer = await link.read([point], after=after, timeout=self._verify_s)
        except (TimeoutError, CapError, Closed):
            return None
        return next((e for e in answer.values if e.quality == "good" and _after(e, after)), None)

    async def _record(
        self,
        ref: str,
        op: Op | Literal["write"],
        params: dict[str, Value],
        who: str,
        why: str | None,
        mode: LeverMode,
        result: Result,
        found: dict[str, dict[str, Any]] | None = None,
        *,
        action: str = "lever.act",
    ) -> Result:
        row = (
            self._clock(),
            ref,
            op,
            json.dumps(params),
            who,
            why,
            mode,
            result.outcome,
            result.detail,
        )

        def write(tx: Transaction) -> None:
            act = tx.execute(
                "INSERT INTO acts (t, lever, op, params, who, why, mode, outcome, detail)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                row,
            ).lastrowid
            if found is not None:
                tx.execute(
                    "INSERT INTO act_found (act, found) VALUES (?, ?)", (act, json.dumps(found))
                )

        await self._db.run(write)
        if (
            result.outcome in COUNTED
            or result.outcome == "dropped"
            or "guard" in (result.detail or "")
        ):
            await self._audit.record(
                who,
                action,
                why=why,
                outcome=result.outcome,
                details={
                    "lever": ref,
                    "op": op,
                    "params": params,
                    "mode": mode,
                    "detail": result.detail,
                },
            )
        if result.outcome == "not_kept":
            log.warning("%s: %s was accepted but not kept (%s)", ref, op, result.detail)
        return result

    # --- What happens without being asked ---------------------------------------------

    def _on_value(self, instance: str, envelope: Envelope) -> None:
        """A setting Thermaestro changed now reads something else: someone else changed
        it."""
        if envelope.quality != "good":
            return
        for ref, claim in list(self.claims.items()):
            if (
                split(ref)[0] != instance
                or claim.mode != "control"
                or claim.drift is not None
                or ref in self._writing
            ):
                continue
            lever = self.lever(ref)
            if lever is None or lever.verify.kind != "readback":
                continue
            if lever.verify.point != envelope.point:
                continue
            observed = (envelope.t_observed or envelope.t_received).timestamp()
            param = lever.params.get("value")
            now = reading(envelope, param)
            if claim.pending is not None:
                since = self._unsettled.get(ref)
                if since is not None and observed > since:
                    drift = self._settle(ref, claim, now, param)
                    if drift is not None:
                        self._spawn(self._told_let_go(ref, claim))
                    else:
                        self._spawn(self._save(claim))
                continue
            if claim.last is None:
                continue
            if claim.last_t is not None and observed <= claim.last_t:
                continue
            if not same(now, claim.last, param):
                self._spawn(self._let_go(ref, f"it reads {now}, not {claim.last} as set"))

    def _on_event(self, instance: str, message: Message) -> None:
        if isinstance(message, Described):
            for described in message.levers:
                ref = f"{instance}:{described.path}"
                claim = self.claims.get(ref)
                if claim is not None and claim.held and claim.drift is None:
                    self._spawn(self._move(ref))
            return
        if not isinstance(message, ForeignWrite):
            return
        for ref, claim in self.claims.items():
            if split(ref)[0] != instance or claim.mode != "control" or claim.drift is not None:
                continue
            lever = self.lever(ref)
            if lever is not None and claim.changed and message.datapoint in lever.touches:
                self._spawn(self._let_go(ref, f"another client wrote {message.datapoint}"))

    async def _let_go(self, ref: str, why: str) -> None:
        claim = self.claims.get(ref)
        if claim is None or claim.drift is not None:
            return
        claim.drift = why
        await self._told_let_go(ref, claim)

    async def _told_let_go(self, ref: str, claim: Claim) -> None:
        """Keep, log and record a lever let go (`claim.drift` set)."""
        await self._save(claim)
        log.warning("%s was changed by something else (%s): let go", ref, claim.drift)
        await self._audit.record("core", "lever.drift", why=claim.drift, details={"lever": ref})

    async def _move(self, ref: str) -> None:
        """An engaged hold its plugin describes anew acts on something else now: release it
        and engage it again, in shadow as in control."""
        why = "what it holds moved: released and engaged again"
        async with self._lock(ref):
            claim = self.claims.get(ref)
            if claim is None or not claim.held or claim.drift is not None:
                return
            released = await self._act(ref, "release", {}, "core", why)
            if released.outcome not in TAKEN | {"shadowed"}:
                log.warning("%s: moving it, the release was %s", ref, released.outcome)
                return
            engaged = await self._act(ref, "engage", {}, "core", why)
            if engaged.outcome not in TAKEN | {"shadowed"}:
                log.warning("%s: moving it, engaging it again was %s", ref, engaged.outcome)

    async def _reconcile(self, ref: str) -> None:
        """A lever left changed by an earlier run that didn't end cleanly: once its plugin
        is up, put it back, unless someone changed it since. Only a reading taken in this
        run says so: a plugin may send one taken before, which says nothing of what the
        earlier run changed. A change that was in flight was taken if the device reads it;
        where it can't be read, it is taken as taken, so it is put back either way. The
        planner then sets what it wants again."""
        while True:
            await asyncio.sleep(self._poll_s)
            claim = self.claims.get(ref)
            if claim is None or claim.drift is not None or not claim.changed:
                return
            lever = self.lever(ref)
            instance, _ = split(ref)
            found = self._host.instances.get(instance)
            if lever is None or found is None or found.link is None:
                continue
            other = self._other_device(claim)
            if other is not None:
                await self._let_go(ref, other)
                return
            pending = claim.pending
            if lever.kind != "hold" and lever.verify.kind == "readback":
                done = await self._since_restart(ref, claim, lever)
                if done is None:
                    continue  # no reading taken in this run yet
                if done:
                    return
            elif pending is not None:
                claim.pending = None
                self._assume(claim, pending["op"], pending["params"])
            await self.restore("Thermaestro restarted without putting this back", [ref])
            return

    async def _since_restart(self, ref: str, claim: Claim, lever: Lever) -> bool | None:
        """What a reading taken in this run says of a lever an earlier run left changed:
        True when it is as found, or someone else changed it (let go); False when it is
        still to be put back; None while there is no such reading."""
        param = lever.params.get("value")
        async with self._lock(ref):
            back = await self._read_since(ref, lever, self._born)
            if self.claims.get(ref) is not claim or claim.drift is not None:
                return True
            if not claim.changed:
                return True
            if back is None:
                return None
            now = reading(back, param)
            pending, claim.pending = claim.pending, None
            self._unsettled.pop(ref, None)
            if pending is not None and same(now, pending["params"].get("value"), param):
                claim.last = None if same(now, claim.baseline, param) else now
            if same(now, claim.baseline, param):
                claim.last, claim.last_t = None, None
                await self._save(claim)
                return True
            if not same(now, claim.last, param):
                was = claim.last if claim.last is not None else claim.baseline
                claim.drift = f"after a restart it reads {now}, not {was}"
                await self._told_let_go(ref, claim)
                return True
            await self._save(claim)
            return False

    async def _watch(self) -> None:
        while True:
            await asyncio.sleep(min(30.0, self._watchdog_s / 4))
            beat = self._beat
            if beat is not None and self._clock() - beat > self._watchdog_s:
                self._beat = None
                log.error("the planner hasn't checked in: putting every lever back")
                try:
                    # What can't be put back now is owed, and tried again (`_retry`).
                    results = await self.restore("the planner stopped answering")
                    if self.events is not None and self.put_back(results):
                        put_back = Raise("restore.watchdog", "house", PUT_BACK)
                        await self.events.moment("restore.watchdog", put_back)
                except Exception:
                    log.exception("the watchdog's restore failed; it keeps watching")

    async def _retry(self) -> None:
        """The levers that couldn't be put back, tried again until they are: in whatever
        mode they are now, and with the planner stopped too."""
        while True:
            await asyncio.sleep(self._poll_s)
            for ref, owed in list(self._owed.items()):
                claim = self.claims.get(ref)
                if claim is None or claim.drift is not None or not claim.changed:
                    await self._paid(ref)
                    continue
                found = self._host.instances.get(split(ref)[0])
                if found is None or found.state is not State.UP or self._clock() < owed.due:
                    continue
                try:
                    await self.restore(owed.why, [ref])
                except Exception:
                    log.exception("putting %s back again failed", ref)

    def _on_up(self, instance: str) -> None:
        """A plugin is back: what it owes is tried at once."""
        for ref, owed in self._owed.items():
            if split(ref)[0] == instance:
                owed.due = 0.0

    async def _owe(self, ref: str, why: str, detail: str) -> None:
        owed = self._owed.setdefault(ref, _Owed(why))
        owed.tries += 1
        doublings = min(owed.tries - 1, RETRY_DOUBLINGS)
        owed.due = self._clock() + min(self._watchdog_s, RETRY_S * 2**doublings)
        log.warning("%s couldn't be put back (%s): it is tried again", ref, detail)
        if self.events is not None:
            args = {"lever": ref, "why": detail}
            what = Raise("lever.not_back", ref, NOT_BACK, args)
            await self.events.raise_(f"lever.not_back:{ref}", what)

    async def _paid(self, ref: str) -> None:
        """A lever owed is put back, taken again, or let go."""
        self._owed.pop(ref, None)
        if self.events is not None:
            await self.events.clear(f"lever.not_back:{ref}")

    async def _leases(self) -> None:
        """Renew leased levers while Thermaestro holds them. Only the core renews, so a
        lease never outlives it."""
        while True:
            await asyncio.sleep(self._poll_s)
            for ref, claim in list(self.claims.items()):
                if claim.mode != "control" or claim.drift is not None or not claim.changed:
                    continue
                if ref in self._owed:  # its lease running out puts it back
                    continue
                lever = self.lever(ref)
                if lever is None:
                    continue
                persistence = assume(lever).persistence
                if persistence.kind != "leased" or persistence.period_s is None:
                    continue
                due = self._renewed.get(ref, claim.last_t or 0.0) + persistence.period_s / 2
                if self._clock() < due:
                    continue
                async with self._lock(ref):
                    result = await self._send(ref, lever, "renew", {})
                self._renewed[ref] = self._clock()
                if result.outcome not in TAKEN:
                    log.warning("%s: renewing its lease: %s", ref, result.outcome)

    # --- Helpers ----------------------------------------------------------------------

    def _lock(self, ref: str) -> asyncio.Lock:
        return self._locks.setdefault(ref, asyncio.Lock())

    def _readback(self, ref: str, lever: Lever) -> Envelope | None:
        if lever.verify.kind != "readback" or lever.verify.point is None:
            return None
        instance, _ = split(ref)
        found = self._values.latest.get(Key(instance, lever.verify.point))
        return found if found is not None and found.quality == "good" else None

    def _unit(self, ref: str) -> dict[str, str] | None:
        """The identity of the device a lever is on, as its plugin describes it: the
        nearest node above the lever that has one."""
        instance, path = split(ref)
        found = self._host.instances.get(instance)
        if found is None or found.described is None:
            return None
        nodes = {n.path: n for n in found.described.nodes}
        node = path.rpartition("/")[0]
        while node:
            identity = nodes[node].identity if node in nodes else None
            if identity is not None:
                fields = ("vendor", "model", "firmware", "serial")
                return {f: v for f in fields if (v := getattr(identity, f))}
            node = node.rpartition("/")[0]
        return None

    def _other_device(self, claim: Claim) -> str | None:
        """Why the device isn't the one the lever was taken over on, or None: a field
        known then and now that differs. One unknown either time isn't a difference."""
        now = self._unit(claim.lever)
        if claim.unit is None or now is None:
            return None
        changed = [
            f"{field} {claim.unit[field]} is now {now[field]}"
            for field in claim.unit
            if field in now and now[field] != claim.unit[field]
        ]
        if not changed:
            return None
        return "the device isn't the one it was taken over on: " + ", ".join(changed)

    def _holds(self, instance: str, condition: str) -> bool:
        """`<point> == <value>` or `!=`, on the point's latest good value."""
        match = _CONDITION.match(condition)
        if match is None:
            return False
        point, operator, literal = match.groups()
        found = self._values.latest.get(Key(instance, point))
        if found is None or found.quality != "good":
            return False
        try:
            expected = json.loads(literal)
        except ValueError:
            expected = literal.strip("'\"")
        # The point's value, or where it shows words ("Auto"), the device's own number.
        equal = same(found.value, expected) or (
            isinstance(found.value, str) and found.raw is not None and same(found.raw, expected)
        )
        return equal if operator == "==" else not equal

    def _for_people(self, ref: str, lever: Lever) -> bool:
        """Whether the vocabulary keeps this lever for a person's explicit action."""
        instance, _ = split(ref)
        node, _, name = lever.path.rpartition("/")
        found = self._host.instances.get(instance)
        kinds = {n.path: n.kind for n in found.described.nodes} if found and found.described else {}
        standard = vocabulary.lever(kinds.get(node, "unit"), name)
        return standard is not None and standard.user_only

    async def _control(self) -> Control:
        return await self._db.get(Control) or Control()

    async def _save(self, claim: Claim) -> None:
        await self._db.run(lambda t: _store_claim(t, claim))

    def _spawn(self, coro: Coroutine[Any, Any, None]) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)


async def left_changed(db: Database) -> list[str]:
    """The levers an earlier run left changed, which a start puts back."""
    claims = await db.run(_load_claims)
    return sorted(
        ref
        for ref, claim in claims.items()
        if claim.mode == "control" and claim.changed and claim.drift is None
    )


def same(a: Value | None, b: Value | None, param: Param | None = None) -> bool:
    """Whether a value read back is the one set: an enum by its name or the device's
    number, a number within half its step."""
    if a is None or b is None:
        return a is b
    if param is not None and param.enum.value is not None:
        names = {str(v): k for k, v in param.enum.value.items()}
        a, b = names.get(str(a), a), names.get(str(b), b)
    if isinstance(a, bool) or isinstance(b, bool):
        return a == b
    if isinstance(a, int | float) and isinstance(b, int | float):
        step = None
        if param is not None and param.range.value is not None:
            step = param.range.value.step
        return abs(a - b) <= (step / 2 if step else 1e-6)
    return str(a) == str(b)


def reading(envelope: Envelope, param: Param | None) -> Value | None:
    """A value read back, in the lever's terms. A point may show a setting in its own words
    ("Normal"), so for an enum the device's number is taken, and named as the lever names
    it; a number the lever doesn't name stays a number."""
    raw = envelope.raw
    if param is None or param.enum.value is None or raw is None or isinstance(raw, str):
        return envelope.value
    names = {str(v): k for k, v in param.enum.value.items()}
    return names.get(str(raw), raw)


def _after(envelope: Envelope, accepted: datetime) -> bool:
    t = envelope.t_observed or envelope.t_received
    return t >= accepted


def _not_before(t: float) -> datetime:
    """`t` as a datetime no earlier than it: one rounded to the microsecond may fall short."""
    at = datetime.fromtimestamp(t, UTC)
    return at if at.timestamp() >= t else at + timedelta(microseconds=1)


def _readable(lever: Lever, op: Op) -> bool:
    """Whether a fresh reading can tell what became of this change."""
    return (
        lever.kind != "hold"
        and lever.verify.kind == "readback"
        and lever.verify.point is not None
        and op in ("set", "feed")
    )


def _owing(claim: Claim) -> bool:
    """Whether a lever is still to be put back from control."""
    return claim.mode == "control" and claim.changed and claim.drift is None


def _load_claims(t: Transaction) -> dict[str, Claim]:
    claims = {}
    for lever, claimed, baseline, mode, held, last, last_t, drift, op, params, unit in t.execute(
        "SELECT claims.lever, claimed, baseline, mode, held, last, last_t, drift, op, params,"
        " claim_unit.identity FROM claims"
        " LEFT JOIN claim_inflight ON claim_inflight.lever = claims.lever"
        " LEFT JOIN claim_unit ON claim_unit.lever = claims.lever"
    ):
        claims[lever] = Claim(
            lever,
            claimed,
            None if baseline is None else json.loads(baseline),
            mode,
            bool(held),
            None if last is None else json.loads(last),
            last_t,
            drift,
            None if op is None else {"op": op, "params": json.loads(params)},
            None if unit is None else json.loads(unit),
        )
    return claims


def _store_claim(t: Transaction, claim: Claim) -> None:
    t.execute(
        "INSERT INTO claims (lever, claimed, baseline, mode, held, last, last_t, drift)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT (lever) DO UPDATE SET"
        " baseline = excluded.baseline, mode = excluded.mode, held = excluded.held,"
        " last = excluded.last, last_t = excluded.last_t, drift = excluded.drift",
        (
            claim.lever,
            claim.claimed,
            None if claim.baseline is None else json.dumps(claim.baseline),
            claim.mode,
            int(claim.held),
            None if claim.last is None else json.dumps(claim.last),
            claim.last_t,
            claim.drift,
        ),
    )
    if claim.pending is None:
        t.execute("DELETE FROM claim_inflight WHERE lever = ?", (claim.lever,))
    else:
        t.execute(
            "INSERT INTO claim_inflight (lever, op, params) VALUES (?, ?, ?)"
            " ON CONFLICT (lever) DO UPDATE SET op = excluded.op, params = excluded.params",
            (claim.lever, claim.pending["op"], json.dumps(claim.pending["params"])),
        )
    if claim.unit is None:
        t.execute("DELETE FROM claim_unit WHERE lever = ?", (claim.lever,))
    else:
        t.execute(
            "INSERT INTO claim_unit (lever, identity) VALUES (?, ?)"
            " ON CONFLICT (lever) DO UPDATE SET identity = excluded.identity",
            (claim.lever, json.dumps(claim.unit, sort_keys=True)),
        )


def _drop_claims(t: Transaction, refs: Collection[str]) -> None:
    for table in ("claims", "claim_inflight", "claim_unit"):
        t.executemany(f"DELETE FROM {table} WHERE lever = ?", [(r,) for r in refs])  # noqa: S608


_COUNTED_JSON = json.dumps(sorted(COUNTED))


def _count(t: Transaction, lever: str, since: float) -> int:
    row = t.execute(
        "SELECT COUNT(*) FROM acts WHERE lever = ? AND t >= ?"
        " AND outcome IN (SELECT value FROM json_each(?))",
        (lever, since, _COUNTED_JSON),
    ).fetchone()
    return int(row[0])


def _count_instance(t: Transaction, instance: str, since: float) -> int:
    prefix = f"{instance}:"
    row = t.execute(
        "SELECT COUNT(*) FROM acts WHERE substr(lever, 1, ?) = ? AND t >= ?"
        " AND outcome IN (SELECT value FROM json_each(?))",
        (len(prefix), prefix, since, _COUNTED_JSON),
    ).fetchone()
    return int(row[0])


def _prune(t: Transaction, before: float) -> None:
    t.execute("DELETE FROM acts WHERE t < ?", (before,))
