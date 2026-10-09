"""The write path: the one way anything is changed on a device.

Every change goes through `Executor.act`. The lever is taken over (claimed) first, with
the value it was found at kept as its baseline; the request is checked, sent and read
back; and what became of it is recorded. In shadow the same checks run and the request is
recorded, but nothing is sent, so what shadow shows is what control would have done.

`restore` puts every lever back as it was found: on the way out, after an input the
decisions rest on goes stale, and when the planner stops answering. A change Thermaestro
didn't make lets go of the lever: it is reported, and never written over.

A hold's release is the plugin's to know (what an emulated block puts back), and a plugin
keeps it across its own restarts. When a plugin describes an engaged hold anew, what it
acts on has changed (the hot-water block follows the mode): it is released and engaged
again.

A lever the vocabulary keeps for people (an alarm reset) is never used by the planner,
the core or an MQTT request.
"""

import asyncio
import contextlib
import json
import logging
import re
from collections.abc import Callable, Collection, Coroutine
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from .. import clock as clocks
from ..cap import Closed, Message, vocabulary
from ..cap.client import CapError, Link
from ..cap.defaults import assume
from ..cap.messages import Described, ForeignWrite, Op
from ..cap.model import Envelope, Lever, Param, Value
from ..store import Control, Database, LeverMode, Transaction
from .audit import AuditLog
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

Outcome = Literal[
    "verified",
    "not_kept",
    "unverifiable",
    "awaiting_effect",
    "timeout",
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
        "shadowed",
        "device_refused",
    }
)
"""Requests that reached the device, or in shadow would have: what the budget and the
guard count. Shadow counts too, so it decides as control would."""

TAKEN: frozenset[str] = frozenset({"verified", "unverifiable", "awaiting_effect", "timeout"})
"""Outcomes after which the device may hold the new value, so a restore must undo it."""

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

    @property
    def changed(self) -> bool:
        """Whether the device may differ from how it was found because of Thermaestro."""
        return self.held or (self.last is not None and not same(self.last, self.baseline))


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
        self._poll_s = poll_s
        self.claims: dict[str, Claim] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._generation: dict[str, int] = {}
        self._writing: set[str] = set()
        self._beat: float | None = None
        self._renewed: dict[str, float] = {}
        self._tasks: set[asyncio.Task[None]] = set()

    # --- Running ------------------------------------------------------------------------

    async def start(self) -> None:
        self.claims = await self._db.run(_load_claims)
        await self._db.run(lambda t: _prune(t, self._clock() - KEEP_ACTS_DAYS * DAY_S))
        self._values.listeners.append(self._on_value)
        self._host.listeners.append(self._on_event)
        for ref, claim in self.claims.items():
            if claim.mode == "control" and claim.changed and claim.drift is None:
                self._spawn(self._reconcile(ref))
        self._spawn(self._watch())
        self._spawn(self._leases())

    async def stop(self) -> None:
        with contextlib.suppress(ValueError):
            self._values.listeners.remove(self._on_value)
        with contextlib.suppress(ValueError):
            self._host.listeners.remove(self._on_event)
        for task in list(self._tasks):
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)

    def heartbeat(self) -> None:
        """The planner checks in after each round. From the first, a planner silent for
        `watchdog_s` has everything put back."""
        self._beat = self._clock()

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

    async def budget(self, instance: str) -> tuple[int, int]:
        """Writes in the last day for an instance's levers, and the soft budget: the
        planner aims to stay under it."""
        control = await self._control()
        since = self._clock() - DAY_S
        used = await self._db.run(lambda t: _count_instance(t, instance, since))
        return used, control.soft_budget

    async def set_mode(self, ref: str, mode: LeverMode, *, who: str) -> None:
        """Put a lever off, in shadow or in control. Leaving control puts it back as it
        was found."""
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
        await self._db.run(lambda t: t.execute("DELETE FROM claims WHERE lever = ?", (ref,)))
        await self._audit.record(who, "lever.drift_accepted", details={"lever": ref})

    async def restore(self, why: str, refs: Collection[str] | None = None) -> dict[str, Result]:
        """Put levers back as they were found: release holds, set settings to their
        baselines. Nothing a person changed meanwhile is written over."""
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
                if claim.held:
                    result = await self._act(ref, "release", {}, "core", why, restoring=True)
                elif claim.baseline is not None:
                    result = await self._act(
                        ref, "set", {"value": claim.baseline}, "core", why, restoring=True
                    )
                else:
                    continue
                if result.outcome == "unchanged":  # already as found
                    claim.held, claim.last, claim.last_t = False, None, None
                    await self._save(claim)
                results[ref] = result
        if results:
            done = TAKEN | {"unchanged"}
            await self._audit.record(
                "core",
                "lever.restore",
                why=why,
                outcome="ok" if all(r.outcome in done for r in results.values()) else "failed",
                details={ref: r.outcome for ref, r in results.items()},
            )
        return results

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
            claim.mode = mode
            claim.held, claim.last, claim.last_t = False, None, None
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
        if mode == "shadow":
            self._took(claim, op, params)
            await self._save(claim)
            return await self._record(
                ref, op, params, who, why, mode, Result("shadowed", "nothing was changed")
            )
        result = await self._send(ref, lever, op, params)
        if result.outcome in TAKEN:
            self._took(claim, op, params)
            await self._save(claim)
        return await self._record(ref, op, params, who, why, mode, result)

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
        if claim.last_t + hold > self._clock():
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
        return found is not None and same(reading(found, param), target, param)

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
            except (Closed, CapError) as e:
                return Result("dropped", f"the plugin's connection: {e}")
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
            return Result("awaiting_effect", check.expectation)
        if op not in ("set", "feed") or check.point is None:
            return Result("unverifiable", "a readback can't show this")
        target = params.get("value")
        param = lever.params.get("value")
        wait = assume(lever).effect_delay_s or self._verify_s
        deadline = clocks.monotonic() + wait
        seen: Value | None = None
        while True:
            with contextlib.suppress(TimeoutError, CapError):
                answer = await link.read([check.point], after=accepted, timeout=max(1.0, wait))
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

    async def _record(
        self,
        ref: str,
        op: Op,
        params: dict[str, Value],
        who: str,
        why: str | None,
        mode: LeverMode,
        result: Result,
    ) -> Result:
        t = self._clock()
        await self._db.run(
            lambda tx: tx.execute(
                "INSERT INTO acts (t, lever, op, params, who, why, mode, outcome, detail)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (t, ref, op, json.dumps(params), who, why, mode, result.outcome, result.detail),
            )
        )
        if (
            result.outcome in COUNTED
            or result.outcome == "dropped"
            or "guard" in (result.detail or "")
        ):
            await self._audit.record(
                who,
                "lever.act",
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
        for ref, claim in self.claims.items():
            if (
                split(ref)[0] != instance
                or claim.mode != "control"
                or claim.drift is not None
                or claim.last is None
                or ref in self._writing
            ):
                continue
            lever = self.lever(ref)
            if lever is None or lever.verify.kind != "readback":
                continue
            if lever.verify.point != envelope.point:
                continue
            observed = (envelope.t_observed or envelope.t_received).timestamp()
            if claim.last_t is not None and observed <= claim.last_t:
                continue
            now = reading(envelope, lever.params.get("value"))
            if not same(now, claim.last, lever.params.get("value")):
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
        await self._save(claim)
        log.warning("%s was changed by something else (%s): let go", ref, why)
        await self._audit.record("core", "lever.drift", why=why, details={"lever": ref})

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
        is up, put it back, unless someone changed it since. The planner then sets what it
        wants again."""
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
            if not claim.held:
                back = self._readback(ref, lever)
                if back is None:
                    continue
                param = lever.params.get("value")
                now = reading(back, param)
                if same(now, claim.baseline, param):
                    claim.last, claim.last_t = None, None
                    await self._save(claim)
                    return
                if not same(now, claim.last, param):
                    await self._let_go(ref, f"after a restart it reads {now}, not {claim.last}")
                    return
            await self.restore("Thermaestro restarted without putting this back", [ref])
            return

    async def _watch(self) -> None:
        while True:
            await asyncio.sleep(min(30.0, self._watchdog_s / 4))
            beat = self._beat
            if beat is not None and self._clock() - beat > self._watchdog_s:
                self._beat = None
                log.error("the planner hasn't checked in: putting every lever back")
                await self.restore("the planner stopped answering")

    async def _leases(self) -> None:
        """Renew leased levers while Thermaestro holds them. Only the core renews, so a
        lease never outlives it."""
        while True:
            await asyncio.sleep(self._poll_s)
            for ref, claim in list(self.claims.items()):
                if claim.mode != "control" or claim.drift is not None or not claim.changed:
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


def _load_claims(t: Transaction) -> dict[str, Claim]:
    claims = {}
    for lever, claimed, baseline, mode, held, last, last_t, drift in t.execute(
        "SELECT lever, claimed, baseline, mode, held, last, last_t, drift FROM claims"
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
        )
    return claims


def _store_claim(t: Transaction, claim: Claim) -> None:
    t.execute(
        "INSERT INTO claims (lever, claimed, baseline, mode, held, last, last_t, drift)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT (lever) DO UPDATE SET"
        " mode = excluded.mode, held = excluded.held, last = excluded.last,"
        " last_t = excluded.last_t, drift = excluded.drift",
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
