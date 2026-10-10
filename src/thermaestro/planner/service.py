"""The planner in the running core: it gathers what the rules need, asks the executor for
what they decide, and follows up on what became of it.

A round runs at every 15-minute slot, and at once when the open intents change. When the
pump's demand changes or the hot water nears its floor, a round between slots plans hot
water only: the heating, the addition and the pool change at most once a slot, from what
the slot's start shows. Between rounds the planner checks in with the executor every
minute; one that stops checking in has everything put back.

A window open, or a room's temperature dropping a degree within ten minutes, pauses the
heat being raised for that room for fifteen minutes: it is airing, not cold.

Following up:
- a value accepted but not kept isn't asked for again for six hours;
- a charge started is judged by its effect: the demand going to hot water within the
  lever's effect delay;
- a "boost now" is met once its charge has run, a bath once the tank reaches it;
- in shadow, what it would have done is kept as a notice, only when that changes.
"""

import asyncio
import json
import logging
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from .. import clock
from ..cap.defaults import assume
from ..cap.model import Envelope, Lever, Value
from ..core.audit import AuditLog
from ..core.executor import HOLD_SLACK_S, Executor, Result, reading, split
from ..core.host import PluginHost
from ..core.house import House
from ..core.prices import assemble
from ..core.sensors import DEFAULT_FRESHNESS_S, SITE, SensorHub
from ..core.series import Series
from ..core.values import Key, Values
from ..intents import Intents
from ..intents.entry import RESPONSE_H
from ..store import Control, Database, Location, PriceLayer, Vat
from . import rules
from .model import SLOT, Decision, LeverState, Memory, Price, RoomReading, Situation, Tank

log = logging.getLogger(__name__)

CHECK_S = 60.0
"""How often the planner checks in, and looks for a reason to plan at once."""
NOT_KEPT_S = 6 * 3600.0
"""How long a value the device accepted but didn't keep isn't asked for again."""
EFFECT_S = 900.0
"""How long a charge's effect is waited for, where the lever names no delay."""
PRICES_S = 300.0
"""How often the prices are worked out again."""
DEADLINES_AHEAD = timedelta(hours=36)
NOTICES = 50
AIRING_DROP = 1.0
AIRING_WITHIN_S = 600.0
AIRING_S = 900.0
"""A room that drops this much this fast is being aired: for this long after, it isn't
heated harder for it."""
SMOOTH_S = 1800.0
"""A room's temperature is steered on by its mean over this long: the air swings with
each run of the compressor, the room doesn't."""
PERIODIC_ABOVE = 3.0
"""How far above its stop temperature the tank's top must rise to count as the pump's
periodic increase."""

PLANNED = ("heating.offset", "block", "boost_once", "stop_temp", "max_power", "start_temp")
"""The levers the rules use, by name."""

Prices = Callable[[datetime], Awaitable[list[Price]]]


@dataclass(frozen=True)
class Asked:
    decision: Decision
    outcome: str
    detail: str | None = None


@dataclass(frozen=True)
class Notice:
    """In shadow: what Thermaestro would have done, and why."""

    t: datetime
    lever: str
    text: str


@dataclass
class Plan:
    t: datetime
    decisions: list[Decision]
    asked: list[Asked] = field(default_factory=list)


@dataclass
class _Effect:
    decision: Decision
    since: datetime
    wait_s: float


class Planner:
    def __init__(
        self,
        db: Database,
        values: Values,
        host: PluginHost,
        executor: Executor,
        intents: Intents,
        house: House,
        audit: AuditLog,
        *,
        sensors: SensorHub | None = None,
        series: Series | None = None,
        prices: Prices | None = None,
        check_s: float = CHECK_S,
    ) -> None:
        self._db = db
        self._values = values
        self._host = host
        self._executor = executor
        self._intents = intents
        self._house = house
        self._audit = audit
        self._sensors = sensors
        self._series = series
        self._prices = prices or self._stack
        self._check_s = check_s
        self.memory = Memory()
        self.plan: Plan | None = None
        self.situation: Situation | None = None
        """What the last full round saw: for the plan page."""
        self.notices: list[Notice] = []
        self._not_kept: dict[str, float] = {}
        self._effects: list[_Effect] = []
        self._fired: dict[str, datetime] = {}
        """Boost-now intents whose charge was asked for, by id."""
        self._charging_since: dict[str, datetime] = {}
        self._last_slot: float | None = None
        self._last_intents: frozenset[str] = frozenset()
        self._last_demand: str | None = None
        self._last_notice: dict[str, str] = {}
        self._floors: dict[str, float] = {}
        """Each tank's floor as the last round saw it."""
        self._near: dict[str, bool] = {}
        """Whether the last round saw the tank near it."""
        self._priced: tuple[float, list[Price]] | None = None
        self._task: asyncio.Task[None] | None = None
        self._situation_levers: dict[str, LeverState] = {}
        self._recent: dict[str, deque[tuple[float, float]]] = {}
        """Each room's temperatures of the last hour, by point."""
        self._aired: dict[str, float] = {}
        """Rooms seen aired, by point: when."""
        values.listeners.append(self._heard)

    # --- running ---------------------------------------------------------------------------

    def start(self) -> None:
        self._task = asyncio.create_task(self.run())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

    async def run(self) -> None:
        while True:
            try:
                await self.check()
            except Exception:
                log.exception("planning failed; trying again")
            await asyncio.sleep(self._check_s)

    async def check(self) -> Plan | None:
        """Check in, and plan if a slot began or something it plans for changed."""
        now = clock.now()
        slot = now.timestamp() // SLOT.total_seconds()
        open_ids = frozenset(i.id for i in await self._intents.all())
        demand = self._value(self._pump_point("demand"))
        due = (
            slot != self._last_slot
            or open_ids != self._last_intents
            or demand != self._last_demand
            or self._near_floor()
        )
        planned = None
        if due:
            full = slot != self._last_slot or open_ids != self._last_intents
            planned = await self.round(slot=full)
            self._last_slot, self._last_intents = slot, open_ids
            self._last_demand = demand if isinstance(demand, str) else None
        else:
            await self._follow_up(now)
        self._executor.heartbeat()
        return planned

    async def round(self, *, slot: bool = True) -> Plan:
        """Gather, decide, ask; the whole of one plan. `slot`: everything is planned, as at
        the start of a slot; else hot water only."""
        sit = replace(await self.gather(), slot=slot)
        self._observe(sit)
        for scope, tank in sit.tanks.items():
            floor = sit.force.bound("tank_top_temp", scope)
            if floor is not None and floor.low is not None:
                self._floors[scope] = floor.low
                near = tank.top is not None and tank.top <= floor.low + rules.FLOOR_MARGIN
                self._near[scope] = near
        decisions = rules.plan(sit)
        self._situation_levers = sit.levers
        if slot:
            self.memory.last_round = sit.now
            self.situation = sit
        plan = Plan(sit.now, decisions)
        for decision in decisions:
            plan.asked.append(await self._ask(decision, sit.now))
        await self._follow_up(sit.now)
        self.plan = plan
        return plan

    # --- what the rules need ---------------------------------------------------------------

    async def gather(self) -> Situation:
        now = clock.now()
        resolver = await self._intents.resolver()
        calendar = resolver.calendar
        caps = self._house.capabilities
        heating = self._heating()
        outdoor = self._number(self._values.latest.get(Key(SITE, "outdoor/temperature")))
        force = resolver.in_force(now, heating=heating, outdoor=outdoor)
        rooms, ahead = [], {}
        for scope, system in sorted(caps.rooms.items()):
            rooms.append(self._room(scope, system, now))
            emitter = caps.emitters.get(system, "unknown")
            later = now + timedelta(hours=RESPONSE_H.get(emitter, RESPONSE_H["unknown"]))
            then = resolver.in_force(later, heating=heating, outdoor=outdoor)
            bound = then.bound("room_temp", scope) or then.bound("room_temp", system)
            ahead[scope] = (bound.low, bound.high) if bound is not None else (None, None)
        tanks = {}
        for scope in sorted(caps.tanks):
            instance, path = split(scope)
            tanks[scope] = Tank(
                scope,
                self._number(self._values.latest.get(Key(instance, f"{path}/temp.top"))),
                self._number(self._values.latest.get(Key(instance, f"{path}/temp.start"))),
                self._number(self._values.latest.get(Key(instance, f"{path}/temp.stop"))),
            )
        pools = {}
        for scope in sorted(caps.pools):
            instance, path = split(scope)
            pools[scope] = self._number(self._values.latest.get(Key(instance, f"{path}/temp")))
        levers = await self._levers()
        budget = None
        for instance in sorted({split(ref)[0] for ref in levers}):
            used, soft = await self._executor.budget(instance)
            if budget is None or used / soft > budget[0] / budget[1]:
                budget = (used, soft)
        boosts = frozenset(
            i.scope
            for i in resolver.intents
            if i.kind == "boost_now" and i.id in force.boost and i.id not in self._fired
        )
        demand = self._value(self._pump_point("demand"))
        return Situation(
            now=now,
            zone=calendar.zone,
            force=force,
            ahead=ahead,
            deadlines=resolver.deadlines(now, now + DEADLINES_AHEAD),
            caps=caps,
            prices=await self._cached_prices(now),
            rooms=rooms,
            tanks=tanks,
            levers=levers,
            heating=heating,
            house_kw=self._house_kw(),
            demand=demand if isinstance(demand, str) else None,
            pools=pools,
            budget=budget,
            boost_scopes=boosts,
            memory=self.memory,
        )

    def _heard(self, instance: str, envelope: Envelope) -> None:
        point = envelope.point
        if instance != SITE or not point.startswith("room.") or not point.endswith("/temperature"):
            return
        value = self._number(envelope)
        if value is None:
            return
        t = (envelope.t_observed or envelope.t_received).timestamp()
        recent = self._recent.setdefault(point, deque())
        if recent and t <= recent[-1][0]:
            return
        recent.append((t, value))
        while recent and recent[0][0] < t - 2 * SMOOTH_S:
            recent.popleft()
        before = [v for s, v in recent if t - AIRING_WITHIN_S <= s < t]
        if before and max(before) - value >= AIRING_DROP:
            self._aired[point] = t

    def _mean(self, point: str, now: datetime) -> float | None:
        """A room's temperature over the last half hour, each reading counted for as long
        as it held."""
        samples = list(self._recent.get(point, ()))
        if not samples:
            return None
        end, start = now.timestamp(), now.timestamp() - SMOOTH_S
        total = weight = 0.0
        for i, (t, value) in enumerate(samples):
            until = samples[i + 1][0] if i + 1 < len(samples) else end
            held = min(until, end) - max(t, start)
            if held > 0:
                total += value * held
                weight += held
        return total / weight if weight > 0 else samples[-1][1]

    def _room(self, scope: str, system: str, now: datetime) -> RoomReading:
        room_id = scope.removeprefix("room:")
        point = f"room.{room_id}/temperature"
        envelope = self._values.latest.get(Key(SITE, point))
        temp = self._mean(point, now) if self._number(envelope) is not None else None
        age = 0.0
        if envelope is not None:
            age = (now - (envelope.t_observed or envelope.t_received)).total_seconds()
        limit = DEFAULT_FRESHNESS_S
        if self._sensors is not None:
            limits = [
                self._sensors.freshness(id)[0]
                for id, s in self._sensors.sensors.items()
                if s.room == room_id and s.quantity == "temperature"
            ]
            limit = max(limits, default=limit)
        name = (
            self._sensors.rooms[room_id].name
            if self._sensors and room_id in self._sensors.rooms
            else ""
        )
        aired = self._aired.get(point)
        window = self._values.latest.get(Key(SITE, f"room.{room_id}/window.open"))
        airing = (aired is not None and now.timestamp() - aired < AIRING_S) or (
            window is not None and window.quality == "good" and window.value is True
        )
        return RoomReading(scope, system, temp, max(0.0, age), limit, name, airing)

    async def _levers(self) -> dict[str, LeverState]:
        """The levers the rules use that are in shadow or in control, and not let go."""
        control = await self._db.get(Control) or Control()
        out = {}
        for instance_id, instance in sorted(self._host.instances.items()):
            if instance.described is None:
                continue
            for lever in instance.described.levers:
                ref = f"{instance_id}:{lever.path}"
                if lever.path.rpartition("/")[2] not in PLANNED or lever.unavailable:
                    continue
                if control.levers.get(ref, "off") == "off":
                    continue
                claim = self._executor.claims.get(ref)
                if claim is not None and claim.drift is not None:
                    continue
                out[ref] = self._lever_state(instance_id, ref, lever, control)
        return out

    def _lever_state(self, instance: str, ref: str, lever: Lever, control: Control) -> LeverState:
        limits = assume(lever).ranges.get("value")
        device: Value | None = None
        if lever.verify.kind == "readback" and lever.verify.point is not None:
            found = self._values.latest.get(Key(instance, lever.verify.point))
            if found is not None and found.quality == "good":
                device = reading(found, lever.params.get("value"))
        claim = self._executor.claims.get(ref)
        current = claim.last if claim is not None and claim.last is not None else device
        baseline = claim.baseline if claim is not None else device
        hold_until = None
        if claim is not None and claim.last_t is not None and lever.kind == "setting":
            hold = control.min_hold_s
            if lever.min_interval_s.trusted:
                hold = max(hold, lever.min_interval_s.value or 0.0)
            hold_until = datetime.fromtimestamp(claim.last_t + hold, UTC)
        return LeverState(
            ref,
            lever.kind,
            low=limits.min if limits else None,
            high=limits.max if limits else None,
            step=limits.step if limits else None,
            current=current,
            baseline=baseline,
            held=claim.held if claim is not None else False,
            hold_until=hold_until,
        )

    def _heating(self) -> bool:
        """Whether the pump's heating stop lets it heat: its mean outdoor temperature below
        its stop. True where it doesn't say."""
        for key, envelope in self._values.latest.items():
            if not key.point.endswith("/heating.stop_temp") or envelope.quality != "good":
                continue
            unit = key.point.rpartition("/")[0]
            mean = self._number(
                self._values.latest.get(Key(key.instance, f"{unit}/outdoor.temp.mean"))
            )
            stop = self._number(envelope)
            if mean is not None and stop is not None and mean >= stop:
                return False
        return True

    def _house_kw(self) -> float | None:
        for key, envelope in sorted(self._values.latest.items(), key=lambda kv: kv[0].instance):
            if key.point.endswith("grid.import.power") and envelope.quality == "good":
                return self._number(envelope)
        return None

    def _pump_point(self, name: str) -> Envelope | None:
        for key, envelope in sorted(self._values.latest.items(), key=lambda kv: kv[0].instance):
            if (
                key.instance != SITE
                and key.point.endswith(f"/{name}")
                and key.point.count("/") == 1
            ):
                return envelope
        return None

    @staticmethod
    def _value(envelope: Envelope | None) -> Value | None:
        return envelope.value if envelope is not None and envelope.quality == "good" else None

    @classmethod
    def _number(cls, envelope: Envelope | None) -> float | None:
        value = cls._value(envelope)
        if isinstance(value, int | float) and not isinstance(value, bool):
            return float(value)
        return None

    def _near_floor(self) -> bool:
        """Whether a tank's top has come near the floor the last round saw, since that
        round: plan at once."""
        for scope, least in self._floors.items():
            instance, path = split(scope)
            top = self._number(self._values.latest.get(Key(instance, f"{path}/temp.top")))
            near = top is not None and top <= least + rules.FLOOR_MARGIN
            if near and not self._near.get(scope, False):
                return True
        return False

    # --- prices ----------------------------------------------------------------------------

    async def _cached_prices(self, now: datetime) -> list[Price]:
        if self._priced is not None and now.timestamp() - self._priced[0] < PRICES_S:
            return self._priced[1]
        try:
            prices = await self._prices(now)
        except Exception:
            log.exception("working out the prices failed: planning without them")
            prices = []
        self._priced = (now.timestamp(), prices)
        return prices

    async def _stack(self, now: datetime) -> list[Price]:
        """The price stack from yesterday to tomorrow, slot by slot, where it has totals."""
        if self._series is None:
            return []
        location = await self._db.get(Location)
        layers = await self._db.all(PriceLayer)
        if location is None or not layers:
            return []
        zone = ZoneInfo(location.timezone)
        vat = await self._db.get(Vat)
        today: date = now.astimezone(zone).date()
        out = []
        for day in (today - timedelta(days=1), today, today + timedelta(days=1)):
            stack = await assemble(layers, vat, self._series, day, zone)
            out += [Price(s.start, s.total) for s in stack.slots if s.total is not None]
        return out

    # --- asking and following up -----------------------------------------------------------

    async def _ask(self, decision: Decision, now: datetime) -> Asked:
        if decision.op == "restore":
            results = await self._executor.restore(decision.reason, [decision.lever])
            result = results.get(decision.lever, Result("unchanged"))
            return Asked(decision, result.outcome, result.detail)
        state = self._situation_levers.get(decision.lever)
        if (
            decision.op == "set"
            and decision.rank > 1
            and state is not None
            and state.hold_until is not None
            and now < state.hold_until - timedelta(seconds=HOLD_SLACK_S)
            and not _same(state.current, decision.params.get("value"))
        ):
            return Asked(decision, "skipped", "changed too recently; asked again next slot")
        key = f"{decision.lever} {decision.op} {json.dumps(decision.params, sort_keys=True)}"
        until = self._not_kept.get(key)
        if until is not None and now.timestamp() < until:
            return Asked(decision, "skipped", "accepted but not kept earlier")
        result = await self._executor.act(
            decision.lever,
            decision.op,  # type: ignore[arg-type]
            dict(decision.params),
            who="planner",
            why=decision.reason,
        )
        if result.outcome == "not_kept":
            self._not_kept[key] = now.timestamp() + NOT_KEPT_S
        elif result.outcome == "awaiting_effect":
            lever = self._executor.lever(decision.lever)
            wait = assume(lever).effect_delay_s if lever is not None else None
            self._effects.append(_Effect(decision, now, wait or EFFECT_S))
        elif result.outcome == "shadowed":
            self._notice(decision, now)
        if decision.op == "fire" and result.outcome in ("awaiting_effect", "shadowed", "verified"):
            for intent in await self._intents.all():
                if intent.kind == "boost_now" and decision.lever.startswith(f"{intent.scope}/"):
                    self._fired.setdefault(intent.id, now)
        return Asked(decision, result.outcome, result.detail)

    def _notice(self, decision: Decision, now: datetime) -> None:
        value = decision.params.get("value")
        what = f"{decision.op} {value:g}" if isinstance(value, int | float) else decision.op
        text = f"would {what} now: {decision.reason}; nothing was changed"
        if self._last_notice.get(decision.lever) == text:
            return
        self._last_notice[decision.lever] = text
        self.notices.append(Notice(now, decision.lever, text))
        del self.notices[:-NOTICES]

    async def _follow_up(self, now: datetime) -> None:
        """Judge the effects waited for, and report intents met."""
        demand = self._value(self._pump_point("demand"))
        for effect in list(self._effects):
            seen = demand == "dhw"
            waited = (now - effect.since).total_seconds()
            if seen or waited > effect.wait_s:
                self._effects.remove(effect)
                await self._audit.record(
                    "planner",
                    "lever.effect",
                    outcome="ok" if seen else "failed",
                    why=effect.decision.reason,
                    details={
                        "lever": effect.decision.lever,
                        "seen": seen,
                        "after_s": round(waited),
                    },
                )
        for intent in await self._intents.all():
            if intent.kind == "boost_now" and intent.id in self._fired:
                since = self._fired[intent.id]
                charged = self._charging_since.get(intent.scope)
                if demand != "dhw" and charged is not None and charged >= since:
                    await self._intents.report(intent.id, "met", "its charge has run")
                    self._fired.pop(intent.id, None)
            elif intent.kind == "bath" and intent.state == "active":
                instance, path = split(intent.scope)
                top = self._number(self._values.latest.get(Key(instance, f"{path}/temp.top")))
                least = next(
                    (
                        float(t.value)
                        for e in intent.expectations
                        for t in e.targets
                        if isinstance(t.value, int | float)
                    ),
                    None,
                )
                if top is not None and least is not None and top >= least:
                    await self._intents.report(intent.id, "met", f"the hot water is {top:.1f} °C")

    def _observe(self, sit: Situation) -> None:
        """Learn from what the pump does: how long its charges take, and when it runs its
        periodic increase."""
        for scope, tank in sit.tanks.items():
            charging = sit.demand == "dhw"
            since = self._charging_since.get(f"{scope}#on")
            if charging and since is None:
                self._charging_since[f"{scope}#on"] = sit.now
            elif not charging and since is not None:
                del self._charging_since[f"{scope}#on"]
                self._charging_since[scope] = since
                took = (sit.now - since).total_seconds()
                if 600 <= took <= 4 * 3600:
                    old = self.memory.charge_s.get(scope, took)
                    self.memory.charge_s[scope] = round(0.7 * old + 0.3 * took)
            top, stop = tank.top, tank.stop
            if top is not None and stop is not None and top >= stop + PERIODIC_ABOVE:
                last = self.memory.periodic.get(scope)
                if last is None or (sit.now - last) > timedelta(days=1):
                    if last is not None:
                        self.memory.periodic[f"{scope}#before"] = last
                    self.memory.periodic[scope] = sit.now


def _same(a: Value | None, b: Value | None) -> bool:
    if isinstance(a, int | float) and isinstance(b, int | float):
        return abs(float(a) - float(b)) < 1e-9
    return a == b
