"""Control: what the household wants (intents and levels, and setup's answers about the
house), what Thermaestro may change (each lever off, in shadow or in control), and what
the planner decided and why. The operations behind the pages and the API.

Asking for an intent needs the right its kind needs; ending someone else's needs
`intent.any.end`. Switching a lever, confirming its competing features off, or keeping a
change made elsewhere needs `levers.control`, with the password entered again.
"""

import json
from datetime import UTC, datetime, tzinfo
from typing import TYPE_CHECKING, Any

from .. import clock
from ..auth import AccountError, Forbidden
from ..auth.permissions import PERMISSIONS
from ..cap.defaults import assume
from ..intents import Forbidden as IntentForbidden
from ..intents import Intent, Level
from ..intents import NotFound as IntentNotFound
from ..intents.requests import Request, build, request_of
from ..store import Control, Home

if TYPE_CHECKING:
    from ..core.audit import AuditLog
    from ..core.executor import Executor
    from ..core.host import PluginHost
    from ..core.sensors import SensorHub
    from ..intents import Intents
    from ..planner import Planner
    from ..store import Database
    from .operations import Caller

MODES = ("off", "shadow", "control")


class ControlOperations:
    """Mixed into the services, whose parts it uses."""

    if TYPE_CHECKING:
        db: Database
        audit: AuditLog
        host: PluginHost | None
        intents: Intents | None
        executor: Executor | None
        planner: Planner | None
        sensors: SensorHub | None
        zone: tzinfo

        def _require(self, caller: Caller, permission: str, *, step_up: bool = False) -> None: ...

        def node_label(
            self, caller: Caller, instance: str, node: str, *, built_in: bool = False
        ) -> str: ...

    # --- intents ---------------------------------------------------------------------------

    def _intents(self) -> "Intents":
        if self.intents is None:
            raise AccountError("intents aren't running here")
        return self.intents

    @staticmethod
    def _granted(caller: "Caller") -> frozenset[str]:
        return frozenset(p for p in PERMISSIONS if caller.principal.allows(p))

    @staticmethod
    def _asker(caller: "Caller") -> str:
        """Who an intent is from: the user, whether asking in a session or with a token."""
        return f"user:{caller.principal.user.name}"

    async def intent_list(self, caller: "Caller", *, ended: bool = False) -> list[Intent]:
        caller.principal.require("plan.read")
        return await self._intents().all(open_only=not ended)

    async def ask(self, caller: "Caller", body: dict[str, Any]) -> dict[str, Any]:
        """Ask for an intent in household terms. The answer says whether it was accepted,
        and what it means: when it ends, what it sets aside, what the house can't do."""
        from .operations import _validated

        request = _validated(Request, body, "the request")
        try:
            intent = build(request, principal=self._asker(caller), now=clock.now())
        except ValueError as e:
            raise AccountError(str(e)) from None
        try:
            verdict = await self._intents().create(intent, granted=self._granted(caller))
        except IntentForbidden as e:
            raise Forbidden(e.right) from None
        return {
            "accepted": verdict.accepted,
            "intent": verdict.intent.model_dump(mode="json"),
            "messages": list(verdict.messages),
        }

    async def edit_intent(self, caller: "Caller", id: str, body: dict[str, Any]) -> dict[str, Any]:
        """Change an intent in place, with the whole request as for asking: answered as
        asking is. Its kind stays; built at its own time, what follows from when it was
        asked (a fireplace's six hours) stays too."""
        from .operations import NotFound, _validated

        intents = self._intents()
        try:
            previous = await intents.get(id)
        except IntentNotFound:
            raise NotFound(f"no intent {id}") from None
        request = _validated(Request, {**body, "kind": previous.kind}, "the request")
        try:
            intent = build(request, principal=previous.principal, now=previous.created)
            verdict = await intents.edit(
                id, intent, who=self._asker(caller), granted=self._granted(caller)
            )
        except ValueError as e:
            raise AccountError(str(e)) from None
        except IntentForbidden as e:
            raise Forbidden(e.right) from None
        return {
            "accepted": verdict.accepted,
            "intent": verdict.intent.model_dump(mode="json"),
            "messages": list(verdict.messages),
        }

    async def understand(self, caller: "Caller", text: str) -> dict[str, Any]:
        """What the household typed, read as a request for a while, with what it still
        needs. Nothing is asked: the request is shown to be checked first."""
        from ..intents.understand import understand

        scopes = self.intent_scopes(caller)
        found = understand(
            text[:500],
            now=clock.now().astimezone(self.zone),
            systems=scopes["systems"],
            tanks=scopes["tanks"],
            levels=await self.level_list(caller),
        )
        return {"kind": found.kind, "request": found.request, "missing": list(found.missing)}

    async def end_intent(self, caller: "Caller", id: str) -> Intent:
        from .operations import NotFound

        try:
            return await self._intents().end(
                id, who=self._asker(caller), granted=self._granted(caller)
            )
        except IntentForbidden as e:
            raise Forbidden(e.right) from None
        except IntentNotFound:
            raise NotFound(f"no intent {id}") from None

    async def confirm_intent(self, caller: "Caller", id: str) -> Intent:
        from .operations import NotFound

        try:
            return await self._intents().confirm(
                id, who=self._asker(caller), granted=self._granted(caller)
            )
        except IntentForbidden as e:
            raise Forbidden(e.right) from None
        except IntentNotFound:
            raise NotFound(f"no intent {id}") from None

    async def in_force(self, caller: "Caller", at: datetime | None = None) -> dict[str, Any]:
        """What applies now: each bound with its edges' ranks and the intents behind it,
        what is paused and why, hands off, and the cost stance."""
        caller.principal.require("plan.read")
        found = await self._intents().in_force(at)
        return {
            "at": found.at.isoformat(),
            "bounds": [
                {
                    "target": b.target,
                    "scope": b.scope,
                    "low": b.low,
                    "high": b.high,
                    "value": b.value,
                    "low_rank": b.low_rank,
                    "high_rank": b.high_rank,
                    "by": list(b.by),
                    "offset": b.offset,
                }
                for _, b in sorted(found.bounds.items())
            ],
            "paused": found.paused,
            "hands_off_until": (
                found.hands_off_until.isoformat() if found.hands_off_until else None
            ),
            "ranking": list(found.ranking),
            "slider": found.slider,
        }

    async def intent_views(self, caller: "Caller") -> list[dict[str, Any]]:
        """The open intents for a page: temporary ones first, each with its end, whether
        it ends when met, and why it is paused if it is."""
        caller.principal.require("plan.read")
        resolver = await self._intents().resolver()
        force = resolver.in_force(clock.now())
        out = []
        for intent in sorted(
            resolver.intents, key=lambda i: (i.tier not in ("temporary", "hands_off"), i.created)
        ):
            end = resolver.end(intent)
            out.append(
                {
                    "intent": intent,
                    "end": end.isoformat() if end else None,
                    "when_met": intent.validity.ends == "when_met",
                    "paused": force.paused.get(intent.id),
                    "request": self._editable(intent),
                }
            )
        return out

    def _editable(self, intent: Intent) -> dict[str, Any] | None:
        """What a form to change an intent starts from, its moments in the house's time;
        None where it can't be put as a request."""
        try:
            found = request_of(intent).model_dump(mode="json", exclude_none=True)
        except (AssertionError, IndexError, ValueError):
            return None
        for name in ("until", "by"):
            if name in found:
                local = datetime.fromisoformat(found[name]).astimezone(self.zone)
                found[name] = local.replace(tzinfo=None).isoformat(timespec="minutes")
        return found

    def intent_scopes(self, caller: "Caller") -> dict[str, list[tuple[str, str]]]:
        """What an intent can name, with each one's name: climate systems, hot-water tanks,
        pools, and rooms."""
        caller.principal.require("plan.read")
        kinds = {"climate_system": "systems", "dhw_tank": "tanks", "pool": "pools"}
        out: dict[str, list[tuple[str, str]]] = {"systems": [], "tanks": [], "pools": []}
        for instance_id, instance in sorted((self.host.instances if self.host else {}).items()):
            for node in instance.described.nodes if instance.described else ():
                if node.kind in kinds:
                    ref = f"{instance_id}:{node.path}"
                    label = self.node_label(caller, instance_id, node.path)
                    out[kinds[node.kind]].append((ref, label))
        rooms = self.sensors.rooms if self.sensors is not None else {}
        out["rooms"] = sorted(
            ((f"room:{id}", r.name) for id, r in rooms.items()), key=lambda x: x[1]
        )
        return out

    async def level_list(self, caller: "Caller") -> dict[str, Level]:
        caller.principal.require("plan.read")
        return await self._intents().levels()

    async def put_level(self, caller: "Caller", id: str, body: dict[str, Any]) -> Level:
        from .operations import _validated

        level = _validated(Level, {**body, "id": id}, "the level")
        try:
            await self._intents().put_level(
                level, who=self._asker(caller), granted=self._granted(caller)
            )
        except IntentForbidden as e:
            raise Forbidden(e.right) from None
        return level

    async def delete_level(self, caller: "Caller", id: str) -> None:
        try:
            await self._intents().delete_level(
                id, who=self._asker(caller), granted=self._granted(caller)
            )
        except IntentForbidden as e:
            raise Forbidden(e.right) from None
        except ValueError as e:
            raise AccountError(str(e)) from None

    # --- setup's answers about the house ---------------------------------------------------

    async def home_settings(self, caller: "Caller") -> Home:
        caller.principal.require("settings.read")
        return await self.db.get(Home) or Home()

    async def set_home(self, caller: "Caller", body: dict[str, Any]) -> Home:
        """Setup's answers: each climate system's emitter, the house, the water, the
        public holidays, and what to do past a missed deadline."""
        self._require(caller, "settings.write")
        from .operations import _validated

        home = _validated(Home, body, "the house")
        await self.db.put(home)
        await self.audit.record(
            caller.principal.name,
            "setting.change",
            source=caller.source,
            details={"kind": "home", **home.model_dump(mode="json")},
        )
        return home

    # --- levers ----------------------------------------------------------------------------

    async def levers(self, caller: "Caller") -> list[dict[str, Any]]:
        """Every lever the plugins offer, with its mode, what it was found at, what
        Thermaestro last set, any change made elsewhere, its competing features and
        whether each is confirmed off, and the day's writes against the budget."""
        caller.principal.require("settings.read")
        control = await self.db.get(Control) or Control()
        executor = self.executor
        out = []
        budgets: dict[str, tuple[int, int]] = {}
        for instance_id, instance in sorted((self.host.instances if self.host else {}).items()):
            if instance.described is None:
                continue
            for lever in instance.described.levers:
                ref = f"{instance_id}:{lever.path}"
                claim = executor.claims.get(ref) if executor is not None else None
                if executor is not None and instance_id not in budgets:
                    budgets[instance_id] = await executor.budget(instance_id)
                used, soft = budgets.get(instance_id, (0, control.soft_budget))
                confirmed = set(control.confirmed_off.get(ref, ()))
                limits = assume(lever).ranges.get("value")
                out.append(
                    {
                        "lever": ref,
                        "kind": lever.kind,
                        "mode": control.levers.get(ref, "off"),
                        "unavailable": lever.unavailable,
                        "works": assume(lever).works,
                        "range": None
                        if limits is None
                        else {"min": limits.min, "max": limits.max, "step": limits.step},
                        "competing": [
                            {"name": f.name, "confirmed_off": f.name in confirmed}
                            for f in lever.competing_features
                        ],
                        "claimed": claim is not None,
                        "baseline": claim.baseline if claim else None,
                        "last": claim.last if claim else None,
                        "held": claim.held if claim else False,
                        "drift": claim.drift if claim else None,
                        "writes_today": used,
                        "budget": soft,
                    }
                )
        return out

    def _lever(self, ref: str) -> None:
        from .operations import NotFound

        if self.executor is None or self.executor.lever(ref) is None:
            raise NotFound(f"no lever {ref}")

    def _executor(self) -> "Executor":
        if self.executor is None:
            raise AccountError("changing settings isn't running here")
        return self.executor

    async def set_lever_mode(self, caller: "Caller", ref: str, mode: str) -> None:
        """Put a lever off, in shadow or in control. Leaving control puts it back as it
        was found."""
        self._require(caller, "levers.control", step_up=True)
        if mode not in MODES:
            raise AccountError(f"a lever is {', '.join(MODES)}")
        self._lever(ref)
        await self._executor().set_mode(ref, mode, who=caller.principal.name)  # type: ignore[arg-type]

    async def confirm_lever_off(self, caller: "Caller", ref: str, features: list[str]) -> None:
        """The household says these competing features of a lever are switched off."""
        self._require(caller, "levers.control", step_up=True)
        self._lever(ref)
        lever = self._executor().lever(ref)
        known = {f.name for f in lever.competing_features} if lever else set()
        unknown = sorted(set(features) - known)
        if unknown:
            raise AccountError(f"{ref} has no competing feature {', '.join(unknown)}")
        await self._executor().confirm_off(ref, features, who=caller.principal.name)

    async def accept_drift(self, caller: "Caller", ref: str) -> None:
        """Keep a change made elsewhere: the lever is let go for good, and taken over
        again from how it is now."""
        self._require(caller, "levers.control", step_up=True)
        self._lever(ref)
        await self._executor().accept_drift(ref, who=caller.principal.name)

    # --- the plan --------------------------------------------------------------------------

    async def recent_acts(self, caller: "Caller", hours: float = 24.0) -> list[dict[str, Any]]:
        """Every change asked in the last `hours`, newest first: made, shadowed or refused,
        with who asked, why, and what became of it."""
        caller.principal.require("plan.read")
        since = clock.time() - hours * 3600

        def read(t: Any) -> list[tuple[Any, ...]]:
            return list(
                t.execute(
                    "SELECT t, lever, op, params, who, why, mode, outcome, detail FROM acts"
                    " WHERE t >= ? ORDER BY t DESC LIMIT 500",
                    (since,),
                )
            )

        rows = await self.db.run(read)
        return [
            {
                "t": datetime.fromtimestamp(t, UTC).isoformat(),
                "lever": lever,
                "op": op,
                "params": json.loads(params),
                "who": who,
                "why": why,
                "mode": mode,
                "outcome": outcome,
                "detail": detail,
            }
            for t, lever, op, params, who, why, mode, outcome, detail in rows
        ]

    def plan(self, caller: "Caller") -> dict[str, Any]:
        """The planner's last round: each decision with its reason and what became of it;
        in shadow, what it would have done; the hot-water deadlines ahead with the window
        chosen for each charge; and the house's power against its limit."""
        from ..planner.rules import CHARGE_S, cheapest_start

        caller.principal.require("plan.read")
        planner = self.planner
        last = planner.plan if planner is not None else None
        sit = planner.situation if planner is not None else None
        ahead = []
        if sit is not None:
            for d in sit.deadlines:
                if d.t <= sit.now:
                    continue
                charge = sit.memory.charge_s.get(d.scope, CHARGE_S)
                start = cheapest_start(sit.prices, sit.now, d.t, charge)
                ahead.append(
                    {
                        "t": d.t.isoformat(),
                        "scope": d.scope,
                        "at_least": d.at_least,
                        "strength": d.strength,
                        "charge_from": start.isoformat() if start else None,
                    }
                )
        limit = sit.force.bound("house_power", "house") if sit is not None else None
        return {
            "ahead": ahead,
            "house_kw": sit.house_kw if sit is not None else None,
            "limit_kw": limit.high if limit is not None else None,
            "ranking": list(sit.force.ranking) if sit is not None else [],
            "at": last.t.isoformat() if last else None,
            "decisions": [
                {
                    "lever": a.decision.lever,
                    "op": a.decision.op,
                    "params": a.decision.params,
                    "rank": a.decision.rank,
                    "reason": a.decision.reason,
                    "outcome": a.outcome,
                    "detail": a.detail,
                }
                for a in (last.asked if last else [])
            ],
            "notices": [
                {"t": n.t.isoformat(), "lever": n.lever, "text": n.text}
                for n in reversed(planner.notices if planner else [])
            ],
        }
