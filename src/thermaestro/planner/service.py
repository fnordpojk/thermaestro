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
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, time, timedelta, tzinfo
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from .. import clock, durations
from ..cap.defaults import assume
from ..cap.model import Envelope, Lever, Value
from ..core.audit import AuditLog
from ..core.events import Events, Raise
from ..core.executor import HOLD_SLACK_S, Executor, Result, reading, split
from ..core.gridrules import GridLimits, household
from ..core.host import PluginHost, State
from ..core.house import House
from ..core.prices import assemble, estimate
from ..core.sensors import DEFAULT_FRESHNESS_S, SITE, SensorHub
from ..core.series import Series
from ..core.values import Key, Values
from ..core.weather import value_at
from ..intents import Capabilities, Intent, Intents
from ..intents.entry import RESPONSE_H, Said, named
from ..intents.resolve import Resolver
from ..learn import Learned, Learnings, mark
from ..learn.energy import charge_kw
from ..learn.forecast import correction
from ..learn.heating import PUMP, RESPONSE
from ..learn.hotwater import expected, house
from ..learn.pump import Response, lead
from ..optimize import Problem
from ..optimize import plan as solve
from ..store import Control, Database, Location, PriceLayer, Transaction, Vat
from . import answers, rules
from .assurance import Finding, judge
from .model import (
    FRESH_S,
    SLOT,
    Decision,
    Effect,
    LeverState,
    Memory,
    Price,
    RoomReading,
    Situation,
    Tank,
)
from .optimizer import TIME_LIMIT_S, Optimized, Optimizer
from .summary import Pump, Summaries
from .watch import MISSED_S, Watch

if TYPE_CHECKING:
    from ..core.weather import Weather

log = logging.getLogger(__name__)

READY_S = 300.0
"""The longest a start waits for the plugins before it plans with what there is."""
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

LEAD_LEAST_H = 2.0
"""A system that answers faster isn't led by the forecast: the pump's curve keeps up."""
LEAD_MOST = 3
"""The most offset steps heating ahead of the weather may add or take."""
PLANNED = ("heating.offset", "block", "boost_once", "stop_temp", "max_power", "start_temp")
"""The levers the rules use, by name."""
CHARGE = "tank.charge_s"
"""How long a tank's charges take, as kept."""
PERIODIC = "tank.periodic"
"""When a tank's periodic increase was last seen, and the time before, as kept."""
PLANNED_KEEP_S = 90 * 86_400.0
"""How long both planners' decisions are kept."""
PLANS_KEEP_S = 14 * 86_400.0
"""How long the optimizer's plans are kept: two weeks of looking back at its predictions."""

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


MISSED_WATER = mark("The hot water was %(top)s °C at its latest, short of %(least)s °C")
KEPT_BAND = mark("Its rooms kept their band throughout")
BELOW_BAND = mark("Its rooms were below their band for %(minutes)s minutes in all")
INTENT_MISSED = mark("%(name_label)s was missed")
INTENT_MISSED_WATER = mark(
    "%(name_label)s was missed: the hot water was %(top)s °C at its latest, short of %(least)s °C"
)
COMFORT_KINDS = frozenset({"warmer", "guests", "away"})
ANSWERED = frozenset({"bath", "hot_water_by", "warmer", "guests", "away", "comfort_band"})
"""The kinds the optimizer's plan can answer for as they are asked."""


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
        learnings: Learnings | None = None,
        weather: "Weather | None" = None,
        events: Events | None = None,
        check_s: float = CHECK_S,
    ) -> None:
        self._weather = weather
        self.events = events
        self._watch = Watch(events, executor, host) if events is not None else None
        self._planning = asyncio.Lock()
        """One plan at a time: a slot's, or one for an answer."""
        self.summaries = Summaries(
            db, values, events, learnings, self._pump_units, self._day_prices
        )
        self._db = db
        self._learnings = learnings
        """Where what the planner observes is kept, so a restart keeps it; in memory only
        without."""
        self._values = values
        self._host = host
        self._executor = executor
        self._intents = intents
        self._house = house
        self._audit = audit
        self._sensors = sensors
        self._series = series
        self._prices = prices or self._stack
        self._given_prices = prices
        self._check_s = check_s
        self.memory = Memory()
        self._memory_kept: str | None = None
        """The memory as last kept, so an unchanged one isn't written again."""
        self._memory_restored = False
        self._began: float | None = None
        """When this planner's first round came: the bound on waiting for the house."""
        self.plan: Plan | None = None
        self.situation: Situation | None = None
        """What the last full round saw: for the plan page."""
        self.notices: list[Notice] = []
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
        self._grid = GridLimits(db, values)
        self.optimizer = (
            Optimizer(db, values, host, learnings, weather) if learnings is not None else None
        )
        if self.optimizer is not None:
            self.optimizer.drift = self.memory.drift
            self.optimizer.memory = self.memory
        """Plans beside the rules every slot; drives where the household chose it."""
        self.optimized: Optimized | None = None
        self.chosen = "rules"
        """The planner the household chose to drive, as the last round read it."""
        self._resolver: Resolver | None = None
        """Each room's temperatures of the last hour, by point."""
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
        if self.situation is not None:
            try:
                await self.summaries.due(now, self.situation.zone)
            except Exception:
                log.exception("the daily summary couldn't be made")
        return planned

    def _pump_units(self) -> list[Pump]:
        """The pump units the summary reads: their compressor's state, and its electrical
        power as the maker documents it."""
        if self.optimizer is None:
            return []
        return [
            Pump(unit, info["compressor"], _rated_kw(info["rating"]))
            for unit, info in self.optimizer._units().items()
        ]

    async def round(self, *, slot: bool = True) -> Plan:
        """Gather, decide, ask; the whole of one plan. `slot`: everything is planned, as at
        the start of a slot; else hot water only."""
        if not self._memory_restored:
            await self.restore_memory(clock.time())
        if self._began is None:
            self._began = clock.time()
        await self._house.refresh()
        if not self._ready():
            # The plugins are still coming up: deciding, judging or reporting now would go
            # by an empty or partial house (rooms not yet read, a pump not yet described).
            return Plan(clock.now(), [])
        sit = replace(await self.gather(), slot=slot)
        await self._observe(sit)
        if self._watch is not None:
            try:
                await self._watch.look(sit)
            except Exception:
                log.exception("watching for events failed this round")
        for scope, tank in sit.tanks.items():
            floor = sit.force.bound("tank_top_temp", scope)
            if floor is not None and floor.low is not None:
                self._floors[scope] = floor.low
                near = tank.top is not None and tank.top <= floor.low + rules.FLOOR_MARGIN
                self._near[scope] = near
        decisions = await self._decide(sit, slot)
        self._situation_levers = sit.levers
        if slot:
            self.memory.last_round = sit.now
            self.situation = sit
        plan = Plan(sit.now, decisions)
        for decision in decisions:
            plan.asked.append(await self._ask(decision, sit.now))
        await self._follow_up(sit.now)
        self.plan = plan
        await self.keep_memory(sit.now.timestamp())
        return plan

    def _ready(self) -> bool:
        """Whether the house is known well enough to plan and judge: every plugin has come
        up and described itself, or failed and is being started again (a plugin truly
        missing is told as such); or `READY_S` has passed since this planner began."""
        if self._began is not None and clock.time() - self._began >= READY_S:
            return True
        instances = self._host.instances.values() if self._host is not None else ()
        for instance in instances:
            if instance.state is State.STARTING:
                return False
            if instance.state is State.UP and instance.described is None:
                return False
        return True

    async def keep_memory(self, now: float) -> None:
        """The memory, kept where a restart finds it; only when it changed."""
        if self._watch is not None:
            self.memory.watch = self._watch.state()
        body = json.dumps(self.memory.body(), sort_keys=True)
        if body == self._memory_kept:
            return
        await self._db.run(
            lambda t: t.execute(
                "INSERT OR REPLACE INTO planner_memory (id, kept, body) VALUES (1, ?, ?)",
                (now, body),
            )
        )
        self._memory_kept = body

    async def restore_memory(self, now: float) -> None:
        """The memory as kept before a restart: whole if kept within `FRESH_S`, else what
        still holds after a stop."""
        self._memory_restored = True
        row = await self._db.run(
            lambda t: t.execute("SELECT kept, body FROM planner_memory WHERE id = 1").fetchone()
        )
        if row is None:
            if self._watch is not None:
                await self._watch.resume()
            return
        kept, body = row
        fresh = now - float(kept) <= FRESH_S
        self.memory = Memory.from_body(json.loads(body), fresh=fresh)
        if self.optimizer is not None:
            self.optimizer.drift = self.memory.drift
            self.optimizer.memory = self.memory
        if self._watch is not None:
            self._watch.load(self.memory.watch, fresh=fresh)
            await self._watch.resume()
        self._memory_kept = body if fresh else None
        log.info(
            "the planner's memory taken up from %s ago%s",
            durations.text(max(0.0, now - float(kept))),
            "" if fresh else ", without its price holds",
        )

    async def _decide(self, sit: Situation, slot: bool) -> list[Decision]:
        """Both planners plan; the one the household chose drives. The optimizer drives
        only the levers whose goals it could plan; the rules the rest, and everything when
        a solve fails. At a slot's start, both planners' decisions are kept."""
        ruled = rules.plan(sit)
        if self.optimizer is None:
            return ruled
        control = await self._db.get(Control) or Control()
        self.chosen = control.planner
        hands_off = sit.force.hands_off_until is not None and sit.now < sit.force.hands_off_until
        optimized: Optimized | None = None
        theirs: list[Decision] = []
        if slot:
            if self._resolver is not None:
                async with self._planning:
                    optimized = await self.optimizer.run(sit, self._resolver)
                self.optimized = optimized
                theirs = [] if hands_off else optimized.decisions
                try:
                    await self._assure(optimized, sit)
                except Exception:
                    log.exception("judging the intents in the plan failed this round")
        elif self.optimizer.last is not None and self.optimizer.last.ok and not hands_off:
            optimized = self.optimizer.last
            theirs = await self.optimizer.between(sit)
        if sit.budget is not None and sit.budget[0] >= sit.budget[1]:
            theirs = [d for d in theirs if d.rank <= rules.WEAR_RANK]
        driving = control.planner == "optimizer" and optimized is not None and optimized.ok
        chosen = ruled
        if driving and optimized is not None:
            chosen = [d for d in ruled if d.lever not in optimized.covered] + theirs
            chosen.sort(key=lambda d: d.rank)
            self._follow(sit, optimized.covered, theirs)
        if slot:
            try:
                await self._keep(sit, ruled, theirs, optimized, control.planner)
            except Exception:
                log.exception("keeping the round's decisions failed")
        return chosen

    async def _assure(self, optimized: Optimized, sit: Situation) -> None:
        """How each intent stands in the optimizer's plan, kept as its state and told as an
        event: at risk, giving way, or back to active once planned met. An intent is
        changed only when its state, or what it gives way to, changes: the plan's figures
        move every slot, and an intent isn't told again for that."""
        if not optimized.ok or optimized.problem is None or optimized.plan is None:
            return  # no plan says anything new: every intent stays as it stands

        def again(problem: Problem) -> Any:
            return solve(problem, TIME_LIMIT_S)

        watched = optimized.watched
        found: dict[str, Finding] = await asyncio.to_thread(
            judge, watched, optimized.problem, optimized.plan, sit.zone, again
        )
        judged = {d.by for d in watched.deadlines} | {
            i for goal in watched.rooms.values() for hour in goal.by for i in hour
        }
        for intent in await self._intents.all():
            if intent.state not in ("active", "at_risk", "giving_way"):
                continue
            key = f"intent.state:{intent.id}"
            finding = found.get(intent.id)
            if finding is None:
                if intent.id not in judged:
                    continue  # not in this plan (its rooms airing or stale): as it stands
                if intent.state != "active":
                    await self._intents.report(intent.id, "active")
                    if self.events is not None:
                        await self.events.clear(key)
                continue
            gives_way_to = intent.why_args.get("what_label")
            if intent.state == finding.state and gives_way_to == finding.args.get("what_label"):
                continue
            await self._intents.report(
                intent.id, finding.state, finding.text(), message=finding.message, args=finding.args
            )
            if self.events is not None:
                what = Raise(
                    f"intent.{finding.state}",
                    intent.scope,
                    finding.message,
                    finding.args,
                    audience="household",
                    principal=intent.principal,
                )
                await self.events.clear(key)
                await self.events.raise_(key, what)
        if self.events is not None:
            # However an intent ended (its time, met, or ended by someone), what was told of
            # it in the plan is over.
            still = {f"intent.state:{i.id}" for i in await self._intents.all()}
            for event in await self.events.open("intent.state:"):
                if event.key not in still:
                    await self.events.clear(event.key)

    async def answer(self, intent: Intent) -> list[Said]:
        """What the optimizer's plan says of an intent just asked for, from a plan made
        with it and kept nowhere: when it will be ready, or how short, and what gives way
        for it."""
        if self.optimizer is None or intent.kind not in ANSWERED:
            return []
        async with self._planning:
            sit = await self.gather()
            resolver = self._resolver
            if resolver is None:
                return []
            preview = await self.optimizer.preview(sit, resolver)
        others = {i.id: i for i in await self._intents.all()}
        return answers.answer(intent, preview, self.optimized, others, sit.zone)

    async def final_report(self, intent: Intent, state: str) -> tuple[str, dict[str, Any]] | None:
        """What came of an intent as it ends: for a bath missed, how warm the hot water was
        against what it asked; for a temporary comfort intent, whether its rooms kept their
        band, and if not, for how long they didn't (a miss counts from its start, half an
        hour before it is raised). One missed is told as an event too."""
        report = await self._final(intent, state)
        if state == "missed" and self.events is not None:
            args: dict[str, Any] = {**named(intent), **(report[1] if report else {})}
            water = report is not None and report[0] == MISSED_WATER
            what = Raise(
                "intent.missed",
                intent.scope,
                INTENT_MISSED_WATER if water else INTENT_MISSED,
                args,
                audience="household",
                principal=intent.principal,
            )
            await self.events.moment(f"intent.missed:{intent.id}", what)
        return report

    async def _final(self, intent: Intent, state: str) -> tuple[str, dict[str, Any]] | None:
        sit = self.situation
        if sit is None:
            return None
        if intent.kind == "bath" and state == "missed":
            least = _asked_top(intent)
            tank = sit.tanks.get(intent.scope)
            if least is None or tank is None or tank.top is None:
                return None
            return MISSED_WATER, {"top": round(tank.top, 1), "least": round(least, 1)}
        if intent.kind not in COMFORT_KINDS or state != "finished" or self.events is None:
            return None
        start = (intent.validity.starts or intent.created).timestamp()
        now = clock.time()
        rooms = {r.scope for r in sit.rooms if intent.scope in ("house", r.scope, r.system)}
        seconds = 0.0
        for event in await self.events.since(start, "comfort.missed:"):
            if event.scope not in rooms:
                continue
            began = max(event.raised - MISSED_S, start)
            ended = min(event.cleared if event.cleared is not None else now, now)
            seconds += max(0.0, ended - began)
        if seconds <= 0:
            return KEPT_BAND, {}
        return BELOW_BAND, {"minutes": round(seconds / 60)}

    def _follow(self, sit: Situation, covered: frozenset[str], theirs: list[Decision]) -> None:
        """While the optimizer drives a climate system's offset, the rules' loop follows
        where it leaves the offset, so that a slot the rules take over picks up from there
        rather than from what the loop would have wound up to."""
        asked = {d.lever: d.params.get("value") for d in theirs if d.op == "set"}
        for system in sit.caps.systems:
            ref = f"{system}/heating.offset"
            lever = sit.levers.get(ref)
            if ref not in covered or lever is None:
                continue
            value = asked.get(ref, lever.current)
            base = rules._number(lever.baseline, rules._number(lever.current))
            if isinstance(value, int | float):
                self.memory.integral[system] = float(value) - base

    async def _keep(
        self,
        sit: Situation,
        ruled: list[Decision],
        theirs: list[Decision],
        optimized: Optimized | None,
        chosen: str,
    ) -> None:
        """Both planners' decisions, with what the devices showed; and the optimizer's
        plan, for the plan ahead and to check against what came."""
        t = sit.now.timestamp()
        ok = optimized is not None and optimized.ok
        covered = optimized.covered if ok and optimized is not None else frozenset()
        rows = []
        for planner, decisions in (("rules", ruled), ("optimizer", theirs)):
            for d in decisions:
                if planner == "rules":
                    driving = chosen == "rules" or not ok or d.lever not in covered
                else:
                    driving = chosen == "optimizer" and ok
                rows.append(
                    (
                        t,
                        planner,
                        int(driving),
                        d.lever,
                        str(d.op),
                        json.dumps(d.params),
                        d.rank,
                        d.reason,
                        json.dumps(self._executor.found(d.lever)),
                    )
                )
        body = plan_body(optimized, sit) if optimized is not None else None

        def write(tx: Transaction) -> None:
            tx.executemany(
                "INSERT INTO planned (t, planner, driving, lever, op, params, rank, why, found)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                rows,
            )
            if body is not None:
                tx.execute(
                    "INSERT OR REPLACE INTO plans (t, driving, body) VALUES (?, ?, ?)",
                    (t, int(chosen == "optimizer" and ok), json.dumps(body)),
                )
            tx.execute("DELETE FROM planned WHERE t < ?", (t - PLANNED_KEEP_S,))
            tx.execute("DELETE FROM plans WHERE t < ?", (t - PLANS_KEEP_S,))

        await self._db.run(write)

    # --- what the rules need ---------------------------------------------------------------

    async def gather(self) -> Situation:
        now = clock.now()
        resolver = await self._intents.resolver()
        self._resolver = resolver
        calendar = resolver.calendar
        caps = self._house.capabilities
        heating = self._heating()
        outdoor = self._number(self._values.latest.get(Key(SITE, "outdoor/temperature")))
        force = resolver.in_force(now, heating=heating, outdoor=outdoor)
        rooms, ahead = [], {}
        for scope, system in sorted(caps.rooms.items()):
            rooms.append(self._room(scope, system, now))
            later = now + timedelta(hours=self._response(system, caps.emitters))
            then = resolver.in_force(later, heating=heating, outdoor=outdoor)
            bound = then.bound("room_temp", scope) or then.bound("room_temp", system)
            ahead[scope] = (bound.low, bound.high) if bound is not None else (None, None)
        tanks = {}
        days = await house(self._db) if self._learnings is not None and caps.tanks else None
        for scope in sorted(caps.tanks):
            self._remembered(scope)
            instance, path = split(scope)
            learned = None
            if self._learnings is not None and days is not None:
                learned = expected(self._learnings, scope, days[0], days[1], now)
            tanks[scope] = Tank(
                scope,
                self._number(self._values.latest.get(Key(instance, f"{path}/temp.top"))),
                self._number(self._values.latest.get(Key(instance, f"{path}/temp.start"))),
                self._number(self._values.latest.get(Key(instance, f"{path}/temp.stop"))),
                loss_per_h=learned[0] if learned else None,
                draws=learned[1] if learned else (),
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
            if i.kind == "boost_now" and i.id in force.boost and i.id not in self.memory.fired
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
            charge_kw=charge_kw(self._learnings) if self._learnings else None,
            grid_limit=await self._grid.now(now, _zone(calendar.zone)),
            demand=demand if isinstance(demand, str) else None,
            pools=pools,
            budget=budget,
            boost_scopes=boosts,
            memory=self.memory,
            response_h=self._responses(caps.systems),
            lead=await self._leads(caps, now, outdoor),
        )

    def _response(self, system: str, emitters: Mapping[str, str]) -> float:
        """How long a system's heat takes to show: learned once proved, else from setup."""
        learned = self._learnings.used(RESPONSE, system) if self._learnings else None
        if isinstance(learned, int | float):
            return float(learned)
        return RESPONSE_H.get(emitters.get(system, "unknown"), RESPONSE_H["unknown"])

    def _responses(self, systems: Iterable[str]) -> dict[str, float]:
        if self._learnings is None:
            return {}
        found = {s: self._learnings.used(RESPONSE, s) for s in systems}
        return {s: float(v) for s, v in found.items() if isinstance(v, int | float)}

    async def _leads(
        self, caps: "Capabilities", now: datetime, outdoor: float | None
    ) -> dict[str, tuple[int, float, float]]:
        """For each climate system without a room sensor, whose pump's curve is learned and
        proved: the offset steps that heat now for the weather one response ahead."""
        if self._learnings is None or self._weather is None:
            return {}
        if outdoor is None:
            outdoor = self._number(self._pump_point("outdoor.temp"))
        if outdoor is None:
            return {}
        sensed = set(caps.rooms.values())
        out = {}
        for system in sorted(caps.systems - sensed):
            curve = self._learnings.used(PUMP, system)
            if not isinstance(curve, dict):
                continue
            hours = self._response(system, caps.emitters)
            if hours < LEAD_LEAST_H:
                continue
            then = now + timedelta(hours=hours)
            try:
                found = await self._weather.forecast("temperature", then, then + timedelta(hours=1))
            except Exception:
                log.exception("the temperature forecast couldn't be read for heating ahead")
                continue
            ahead = value_at(found.spans, then, True)
            if ahead is None:
                continue
            ahead -= correction(self._learnings, found.source, hours)
            response = Response(curve.get("c0", 0.0), curve.get("c1", 0.0), curve.get("c2", 1.0), 0)
            if response.c2 <= 0:
                continue
            steps = lead(response, outdoor, ahead, LEAD_MOST)
            out[system] = (steps, ahead, hours)
        return out

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
            self.memory.aired[point] = t

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
        aired = self.memory.aired.get(point)
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
        found = self._grid.house_power()
        if found is None:
            return None
        value = self._number(self._values.latest.get(found[0]))
        return None if value is None else value * found[1]

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
        """The price stack from yesterday to the day after tomorrow, slot by slot, where it
        has totals; what isn't published yet estimated, and marked so."""
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
        for days in range(-1, 3):
            day = today + timedelta(days=days)
            out += await self._day_stack(day, zone, layers, vat, estimated=days >= 0)
        return out

    async def _day_stack(
        self,
        day: date,
        zone: ZoneInfo,
        layers: Mapping[str, PriceLayer],
        vat: Vat | None,
        *,
        estimated: bool,
    ) -> list[Price]:
        assert self._series is not None  # noqa: S101 - the caller checked
        rules, holiday = await household(self._db, zone)
        stack = await assemble(layers, vat, self._series, day, zone, rules, holiday)
        if estimated:
            stack = await estimate(layers, vat, self._series, stack, zone)
        return [Price(s.start, s.total, s.estimated) for s in stack.slots if s.total is not None]

    async def _day_prices(self, day: date, zone: tzinfo) -> list[Price]:
        """A past day's prices, as published, for its summary."""
        start = datetime.combine(day, time(0), zone)
        end = start + timedelta(days=1)
        if self._given_prices is not None:
            return [p for p in await self._prices(clock.now()) if start <= p.start < end]
        location = await self._db.get(Location)
        if self._series is None or location is None:
            return []
        layers = await self._db.all(PriceLayer)
        if not layers:
            return []
        vat = await self._db.get(Vat)
        house = ZoneInfo(location.timezone)
        return await self._day_stack(day, house, layers, vat, estimated=False)

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
        until = self.memory.not_kept.get(key)
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
            self.memory.not_kept[key] = now.timestamp() + NOT_KEPT_S
        elif result.outcome == "awaiting_effect":
            lever = self._executor.lever(decision.lever)
            wait = assume(lever).effect_delay_s if lever is not None else None
            effect = Effect(decision.lever, decision.reason, now, wait or EFFECT_S)
            self.memory.effects.append(effect)
        elif result.outcome == "shadowed":
            self._notice(decision, now)
        if decision.op == "fire" and result.outcome in ("awaiting_effect", "shadowed", "verified"):
            for intent in await self._intents.all():
                if intent.kind == "boost_now" and decision.lever.startswith(f"{intent.scope}/"):
                    self.memory.fired.setdefault(intent.id, now)
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
        for effect in list(self.memory.effects):
            seen = demand == "dhw"
            waited = (now - effect.since).total_seconds()
            if seen or waited > effect.wait_s:
                self.memory.effects.remove(effect)
                await self._audit.record(
                    "planner",
                    "lever.effect",
                    outcome="ok" if seen else "failed",
                    why=effect.reason,
                    details={
                        "lever": effect.lever,
                        "seen": seen,
                        "after_s": round(waited),
                    },
                )
        for intent in await self._intents.all():
            if intent.kind == "boost_now" and intent.id in self.memory.fired:
                since = self.memory.fired[intent.id]
                charged = self.memory.charging_since.get(intent.scope)
                if demand != "dhw" and charged is not None and charged >= since:
                    await self._intents.report(intent.id, "met", "its charge has run")
                    self.memory.fired.pop(intent.id, None)
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

    async def _observe(self, sit: Situation) -> None:
        """Learn from what the pump does: how long its charges take, and when it runs its
        periodic increase."""
        for scope, tank in sit.tanks.items():
            charging = sit.demand == "dhw"
            since = self.memory.charging_since.get(f"{scope}#on")
            if charging and since is None:
                self.memory.charging_since[f"{scope}#on"] = sit.now
            elif not charging and since is not None:
                del self.memory.charging_since[f"{scope}#on"]
                self.memory.charging_since[scope] = since
                took = (sit.now - since).total_seconds()
                if 600 <= took <= 4 * 3600:
                    await self._charged(scope, took, since)
            top, stop = tank.top, tank.stop
            if top is not None and stop is not None and top >= stop + PERIODIC_ABOVE:
                last = self.memory.periodic.get(scope)
                if last is None or (sit.now - last) > timedelta(days=1):
                    if last is not None:
                        self.memory.periodic[f"{scope}#before"] = last
                    self.memory.periodic[scope] = sit.now
                    await self._keep_periodic(scope)

    async def _charged(self, scope: str, took: float, since: datetime) -> None:
        """A charge ran for `took` seconds: blend it into how long the tank's charges take."""
        found = self._learnings.get(CHARGE, scope) if self._learnings is not None else None
        if found is None or not isinstance(found.value, int | float):
            old = self.memory.charge_s.get(scope, took) if found is None else took
            n, first = 0, since.timestamp()
        else:
            old, n, first = float(found.value), found.n, found.since or since.timestamp()
        learned = round(0.7 * old + 0.3 * took)
        self.memory.charge_s[scope] = learned
        if self._learnings is None:
            return
        kept = await self._learnings.put(
            Learned(
                name=CHARGE,
                scope=scope,
                kind="observed",
                value=float(learned),
                unit="s",
                state="learning",
                why=mark("The mean of the charges seen, the newest weighing most."),
                n=n + 1,
                since=first,
            )
        )
        used = kept.used
        if isinstance(used, int | float):
            self.memory.charge_s[scope] = float(used)

    async def _keep_periodic(self, scope: str) -> None:
        if self._learnings is None:
            return
        seen = {
            k: self.memory.periodic[f"{scope}{suffix}"].timestamp()
            for k, suffix in (("last", ""), ("before", "#before"))
            if f"{scope}{suffix}" in self.memory.periodic
        }
        await self._learnings.put(
            Learned(
                name=PERIODIC,
                scope=scope,
                kind="observed",
                value=seen,
                state="learning",
                why=mark("When the tank was last seen well above its stop temperature."),
                n=len(seen),
                since=min(seen.values()),
            )
        )

    def _remembered(self, scope: str) -> None:
        """What is kept of a tank, into the memory the rules read: the household may have
        started it over or pinned it since."""
        if self._learnings is None:
            return
        used = self._learnings.used(CHARGE, scope)
        if isinstance(used, int | float):
            self.memory.charge_s[scope] = float(used)
        else:
            self.memory.charge_s.pop(scope, None)
        periodic = self._learnings.used(PERIODIC, scope)
        for k, suffix in (("last", ""), ("before", "#before")):
            if isinstance(periodic, dict) and k in periodic:
                self.memory.periodic[f"{scope}{suffix}"] = datetime.fromtimestamp(periodic[k], UTC)
            else:
                self.memory.periodic.pop(f"{scope}{suffix}", None)


def _asked_top(intent: Intent) -> float | None:
    """The hot water an intent asks for, at the top of the tank."""
    for expectation in intent.expectations:
        for target in expectation.targets:
            if target.name == "tank_top_temp" and isinstance(target.value, int | float):
                return float(target.value)
    return None


def _same(a: Value | None, b: Value | None) -> bool:
    if isinstance(a, int | float) and isinstance(b, int | float):
        return abs(float(a) - float(b)) < 1e-9
    return a == b


def _zone(found: tzinfo) -> ZoneInfo:
    return found if isinstance(found, ZoneInfo) else ZoneInfo("UTC")


def plan_body(optimized: Optimized, sit: Situation) -> dict[str, Any]:
    """The optimizer's plan, hour by hour, compact: kept for the plan ahead and to check its
    predictions against what came."""
    out: dict[str, Any] = {
        "ok": optimized.ok,
        "why": optimized.why,
        "notes": optimized.notes,
        "covered": sorted(optimized.covered),
    }
    problem, found = optimized.problem, optimized.plan
    if problem is None or found is None or not found.ok:
        return out
    slots = problem.slots
    hours = len(slots) // 4

    def hourly(values: list[float], digits: int = 2) -> list[float]:
        return [round(sum(values[4 * h : 4 * h + 4]) / 4, digits) for h in range(hours)]

    def ends(values: list[float]) -> list[float]:
        return [round(values[4 * h + 3], 2) for h in range(hours)]

    prices = [s.price for s in slots]
    out.update(
        {
            "t0": slots[0].t,
            "cost": round(found.cost, 3),
            "stages": found.stages,
            "widened": found.widened,
            "seconds": round(found.seconds, 3),
            "price": [
                None
                if any(p is None for p in prices[4 * h : 4 * h + 4])
                else round(sum(p or 0.0 for p in prices[4 * h : 4 * h + 4]) / 4, 4)
                for h in range(hours)
            ],
            "estimated": [any(s.estimated for s in slots[4 * h : 4 * h + 4]) for h in range(hours)],
            "outdoor": hourly([s.outdoor for s in slots], 1),
            "kw": hourly(found.kw),
            "systems": {
                scope: {
                    "offset": [round(v, 2) for v in sp.offset],
                    "room": [round(v, 2) for v in sp.room],
                    "heat": [round(v, 2) for v in sp.heat_kw],
                }
                for scope, sp in found.systems.items()
            },
            "tanks": {
                scope: {"level": ends(tp.level), "charge": hourly(tp.share)}
                for scope, tp in found.tanks.items()
            },
            "pools": {
                scope: {"temp": ends(pp.temp), "heat": hourly(pp.share)}
                for scope, pp in found.pools.items()
            },
        }
    )
    return out


def _rated_kw(rating: Any) -> dict[str, float] | None:
    """The compressor's electrical power by its maker's figures: heating at the lowest
    supply temperature documented, hot water at the highest."""
    points = [p for p in getattr(rating, "heating", ()) if p.elec_kw]
    if not points:
        return None
    low = min(points, key=lambda p: p.supply_c)
    high = max(points, key=lambda p: p.supply_c)
    return {"heating": float(low.elec_kw), "dhw": float(high.elec_kw)}
