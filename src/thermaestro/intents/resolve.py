"""What is in force: the intents merged into one bound per target and scope, with the
rank each edge has, who set it, and what is paused and why.

The ladder, highest first: protection (the hot-water floor), hands off, temporary,
standing, and the defaults (seeded or learned, not yet confirmed), which apply only where
nothing standing does. Among temporary intents on the same target the newest wins; a
"warmer" is an offset on whatever band is in force below it, an "away" or "guests"
replaces the band. Compatible intents of one tier intersect: the highest floor, the
lowest ceiling. Deadlines aren't bounds: `deadlines` lists them over a span.
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from datetime import date, datetime, time, timedelta

from .calendar import Calendar
from .model import Context, Intent, Level, Target, TargetName

RANKS = ("comfort_low", "must_deadlines", "should_deadlines", "comfort_high", "power_peak")
"""The default order of what gives way, from rank 2 down; rank 1 is the protection
floors, and the savings slider comes last."""
PROTECTION = 1
NEXT_CHANGE_SPAN = timedelta(hours=24)
"""How far ahead an intent that ends at the next change looks for that change: it lasts
at most a day, also where the pattern doesn't change that soon or ever."""
STEP = timedelta(minutes=15)
COMFORT: frozenset[str] = frozenset({"room_temp", "offset_shift"})
REPLACING = frozenset({"guests", "away"})


@dataclass(frozen=True)
class Bound:
    target: TargetName
    scope: str
    low: float | None = None
    high: float | None = None
    value: float | str | bool | None = None
    low_rank: int | None = None
    high_rank: int | None = None
    by: tuple[str, ...] = ()
    """The intents it comes from."""
    offset: float = 0.0
    """A "warmer" or "cooler" on top, already in `low` and `high`."""

    def merged(self, other: "Bound") -> "Bound":
        """Two bounds of one tier together: the highest floor, the lowest ceiling."""
        low = max((x for x in (self.low, other.low) if x is not None), default=None)
        high = min((x for x in (self.high, other.high) if x is not None), default=None)
        return replace(
            self,
            low=low,
            high=high,
            value=other.value if other.value is not None else self.value,
            by=self.by + other.by,
        )


@dataclass(frozen=True)
class Deadline:
    t: datetime
    scope: str
    at_least: float
    strength: str
    rank: int
    by: str


@dataclass
class InForce:
    at: datetime
    bounds: dict[tuple[str, str], Bound] = field(default_factory=dict)
    paused: dict[str, str] = field(default_factory=dict)
    """Intent id to why it doesn't apply now: shadowed, hands off, the heating stop."""
    hands_off_until: datetime | None = None
    fireplace: frozenset[str] = frozenset()
    boost: tuple[str, ...] = ()
    """Boost-now intents still waiting for their charge."""
    ranking: tuple[str, ...] = RANKS
    slider: float | None = None
    """None: no cost stance, so no shifting by price."""

    def rank(self, item: str) -> int:
        return 2 + self.ranking.index(item)

    def bound(self, target: str, scope: str) -> Bound | None:
        return self.bounds.get((target, scope))


@dataclass
class Resolver:
    intents: list[Intent]
    levels: Mapping[str, Level]
    calendar: Calendar = field(default_factory=Calendar)
    rooms: Mapping[str, str] = field(default_factory=dict)
    """Room scope to its climate system's: a room inherits the system's band and its
    temporary intents."""
    _ends: dict[str, datetime] = field(default_factory=dict)

    # --- when -----------------------------------------------------------------------------

    def end(self, intent: Intent) -> datetime | None:
        """When an intent stops applying, by its validity; None: never."""
        v = intent.validity
        if v.ends in ("at", "when_met"):
            return v.at
        if v.ends == "never":
            return None
        if intent.id not in self._ends:
            self._ends[intent.id] = self._next_change(intent)
        return self._ends[intent.id]

    def live(self, intent: Intent, at: datetime) -> bool:
        if not intent.open:
            return False
        start = intent.validity.starts or intent.created
        end = self.end(intent)
        return start <= at and (end is None or at < end)

    def holds(self, context: Context, at: datetime, outdoor: float | None) -> bool:
        """Whether a context holds at a moment. A deadline's context holds throughout its
        day: what it asks is in force until its time."""
        local = self.calendar.local(at)
        weekday = self.calendar.weekday(local.date())
        if context.at is not None:
            return at <= context.at
        if context.days and weekday not in context.days:
            return False
        if context.season is not None and not _in_season(local.date(), context.season):
            return False
        if context.outdoor_below is not None and (
            outdoor is None or outdoor >= context.outdoor_below
        ):
            return False
        if context.outdoor_above is not None and (
            outdoor is None or outdoor <= context.outdoor_above
        ):
            return False
        if context.start is not None or context.end is not None:
            return _in_span(local.time(), context.start or time(0), context.end or time(0))
        return True

    def applies(self, intent: Intent, at: datetime, outdoor: float | None) -> list[Target]:
        """The targets an intent's expectations ask for at a moment."""
        out: list[Target] = []
        for expectation in intent.expectations:
            spans = [c for c in expectation.contexts if not c.deadline]
            if expectation.contexts and not spans:
                continue  # deadlines only: see `deadlines`
            if not spans or any(self.holds(c, at, outdoor) for c in spans):
                out.extend(expectation.targets)
        return out

    # --- the bounds ------------------------------------------------------------------------

    def in_force(
        self, at: datetime, *, heating: bool = True, outdoor: float | None = None
    ) -> InForce:
        """What applies at `at`. `heating`: whether the pump's heating stop has heating on;
        while off, comfort is scheduled, not in force."""
        result = InForce(at)
        live = [i for i in self.intents if self.live(i, at)]
        result.ranking, result.slider = _stance(live)
        for intent in live:
            if intent.kind == "hands_off":
                end = self.end(intent)
                if end is not None and (
                    result.hands_off_until is None or end > result.hands_off_until
                ):
                    result.hands_off_until = end
        result.fireplace = frozenset(i.scope for i in live if i.kind == "fireplace")
        result.boost = tuple(i.id for i in live if i.kind == "boost_now")
        standing = self._standing(live, at, outdoor, result)
        if not heating:
            for key in [k for k in standing if k[0] in COMFORT]:
                for intent_id in standing.pop(key).by:
                    result.paused[intent_id] = "the pump's heating stop has heating off"
        temporary = [i for i in live if i.tier == "temporary" and i.kind in ("warmer", *REPLACING)]
        for key, bound in standing.items():
            if key[0] in COMFORT and heating:
                bound = self._temporary(key, bound, temporary, result, at)
            result.bounds[key] = bound
        return result

    def _standing(
        self, live: list[Intent], at: datetime, outdoor: float | None, result: InForce
    ) -> dict[tuple[str, str], Bound]:
        """The standing layer: confirmed intents, then the defaults where they leave a gap;
        the protection floors over both."""
        layers: dict[str, dict[tuple[str, str], Bound]] = {"standing": {}, "default": {}}
        floors: dict[tuple[str, str], Bound] = {}
        for intent in live:
            if intent.tier in ("temporary", "hands_off"):
                continue
            for target in self.applies(intent, at, outdoor):
                bound = self._bound(intent, target, result)
                if bound is None:
                    continue
                key: tuple[str, str] = (target.name, intent.scope)
                if intent.kind == "hot_water_floor":
                    layer = floors
                else:
                    layer = layers["standing" if intent.confirmed else "default"]
                layer[key] = layer[key].merged(bound) if key in layer else bound
        out = dict(layers["default"])
        out.update(layers["standing"])
        for key, bound in floors.items():
            out[key] = out[key].merged(bound) if key in out else bound
        # A room without a band of its own has its climate system's.
        for room, system in self.rooms.items():
            for name in COMFORT:
                if (name, room) not in out and (name, system) in out:
                    out[(name, room)] = replace(out[(name, system)], scope=room)
        return out

    def _bound(self, intent: Intent, target: Target, result: InForce) -> Bound | None:
        low, high, value = target.low, target.high, target.value
        if target.level is not None:
            level = self.levels.get(target.level)
            if level is None:
                return None
            low, high = (level.low, level.high) if level.top is None else (level.top, None)
        if target.condition == "at_least":
            low, high = (float(value) if isinstance(value, int | float) else low), None
        elif target.condition == "at_most":
            low, high = None, (float(value) if isinstance(value, int | float) else high)
        low_rank = high_rank = None
        if intent.kind == "hot_water_floor":
            low_rank = PROTECTION
        elif target.name in COMFORT:
            low_rank, high_rank = result.rank("comfort_low"), result.rank("comfort_high")
        elif target.name == "house_power":
            high_rank = result.rank("power_peak")
        return Bound(
            target.name,
            intent.scope,
            low=low,
            high=high,
            value=value if target.condition == "equals" else None,
            low_rank=low_rank,
            high_rank=high_rank,
            by=(intent.id,),
        )

    def _covers(self, intent: Intent, scope: str) -> bool:
        return intent.scope in ("house", scope, self.rooms.get(scope))

    def _temporary(
        self,
        key: tuple[str, str],
        bound: Bound,
        temporary: list[Intent],
        result: InForce,
        at: datetime,
    ) -> Bound:
        """The temporary stack on a comfort target: the newest wins."""
        stack = sorted(
            (i for i in temporary if self._covers(i, key[1])),
            key=lambda i: i.created,
            reverse=True,
        )
        if not stack:
            return bound
        newest = stack[0]
        base = bound
        replacing = next((i for i in stack if i.kind in REPLACING), None)
        if replacing is not None:
            level = self._level_for(replacing, key[1])
            if level is not None and level.low is not None:
                base = replace(bound, low=level.low, high=level.high, by=(replacing.id,))
                end = self.end(replacing)
                until = f" until {self.calendar.local(end):%Y-%m-%d %H:%M}" if end else ""
                for standing in bound.by:
                    result.paused[standing] = (
                        f"shadowed by {replacing.kind} ({replacing.principal}){until}"
                    )
        winner = newest
        if newest.kind == "warmer":
            delta = newest.parameters.get("offset")
            shift = float(delta) if isinstance(delta, int | float) else 0.0
            base = replace(
                base,
                low=None if base.low is None else base.low + shift,
                high=None if base.high is None else base.high + shift,
                offset=shift,
                by=(*base.by, newest.id),
            )
        for intent in stack:
            if intent is winner or (newest.kind == "warmer" and intent is replacing):
                continue
            end = self.end(winner)
            until = f" until {self.calendar.local(end):%Y-%m-%d %H:%M}" if end else ""
            result.paused[intent.id] = f"shadowed by {winner.kind} ({winner.principal}){until}"
        return base

    def _level_for(self, intent: Intent, scope: str) -> Level | None:
        levels = intent.parameters.get("levels")
        if not isinstance(levels, dict):
            return None
        chosen = levels.get(scope) or levels.get(self.rooms.get(scope, ""))
        return self.levels.get(chosen) if isinstance(chosen, str) else None

    # --- deadlines -------------------------------------------------------------------------

    def deadlines(self, start: datetime, end: datetime) -> list[Deadline]:
        """The hot-water deadlines between `start` and `end`, in time order. During an
        "away" only the floor holds, and the house is ready by its return."""
        out: list[Deadline] = []
        result = InForce(start)
        result.ranking, _ = _stance([i for i in self.intents if self.live(i, start)])
        aways = [i for i in self.intents if i.kind == "away" and i.open]
        guests = [i for i in self.intents if i.kind == "guests" and i.open]
        for intent in self.intents:
            if not intent.open or intent.kind not in ("hot_water_by", "bath"):
                continue
            strength = intent.strength or ("must" if intent.kind == "bath" else "should")
            rank = result.rank("must_deadlines" if strength == "must" else "should_deadlines")
            for expectation in intent.expectations:
                for context in expectation.contexts:
                    for t in self._moments(context, start, end):
                        if not self.live(intent, t - timedelta(seconds=1)):
                            continue
                        if intent.kind == "hot_water_by" and any(self.live(a, t) for a in aways):
                            continue
                        for target in expectation.targets:
                            least = self._at_least(target)
                            if least is None:
                                continue
                            for guest in guests:
                                if self.live(guest, t):
                                    more = self._guest_hot_water(guest)
                                    least = max(least, more) if more is not None else least
                            out.append(Deadline(t, intent.scope, least, strength, rank, intent.id))
        for away in aways:
            back = self.end(away)
            if back is None or not start <= back < end:
                continue
            tops = [d.at_least for d in self._standing_deadlines_around(back)]
            if tops:
                rank = result.rank("should_deadlines")
                out.append(Deadline(back, "house", max(tops), "should", rank, away.id))
        return sorted(out, key=lambda d: d.t)

    def _standing_deadlines_around(self, t: datetime) -> list[Deadline]:
        """The standing hot-water deadlines of the day around `t`, ignoring any away."""
        others = Resolver(
            [i for i in self.intents if i.kind == "hot_water_by"], self.levels, self.calendar
        )
        return others.deadlines(t - timedelta(hours=12), t + timedelta(hours=12))

    def _moments(self, context: Context, start: datetime, end: datetime) -> Iterable[datetime]:
        if context.at is not None:
            if start <= context.at < end:
                yield context.at
            return
        if context.by is None:
            return
        day = self.calendar.local(start).date()
        last = self.calendar.local(end).date()
        while day <= last:
            weekday = self.calendar.weekday(day)
            if (not context.days or weekday in context.days) and (
                context.season is None or _in_season(day, context.season)
            ):
                t = datetime.combine(day, context.by, self.calendar.zone)
                if start <= t < end:
                    yield t
            day += timedelta(days=1)

    def _at_least(self, target: Target) -> float | None:
        if target.level is not None:
            level = self.levels.get(target.level)
            return None if level is None else level.top if level.top is not None else level.low
        return float(target.value) if isinstance(target.value, int | float) else target.low

    def _guest_hot_water(self, guest: Intent) -> float | None:
        level = self.levels.get(str(guest.parameters.get("hot_water")))
        return None if level is None else level.top

    # --- the next change -------------------------------------------------------------------

    def _next_change(self, intent: Intent) -> datetime:
        """When the standing pattern for the intent's target next changes, after it was
        made; at most NEXT_CHANGE_SPAN later."""
        target = "room_temp"
        scope = intent.scope
        standing = Resolver(
            [i for i in self.intents if i.tier in ("standing", "default", "protection")],
            self.levels,
            self.calendar,
            self.rooms,
        )
        start = intent.validity.starts or intent.created
        t = start
        first = _key_bound(standing, t, target, scope)
        while t < start + NEXT_CHANGE_SPAN:
            t = t + STEP - timedelta(seconds=t.timestamp() % STEP.total_seconds())
            if _key_bound(standing, t, target, scope) != first:
                return t
        return start + NEXT_CHANGE_SPAN


def _key_bound(
    resolver: Resolver, t: datetime, target: str, scope: str
) -> tuple[float | None, float | None] | None:
    found = resolver.in_force(t)
    for key, bound in found.bounds.items():
        if key[0] == target and (scope == "house" or key[1] == scope):
            return bound.low, bound.high
    return None


def _newest(intents: Iterable[Intent]) -> Intent | None:
    return max(intents, key=lambda i: i.created, default=None)


def _stance(live: list[Intent]) -> tuple[tuple[str, ...], float | None]:
    """The ranking and the slider of the cost stance in force: a confirmed one before a
    seeded one. Without one, the default ranking and no shifting by price."""
    stance = _newest(i for i in live if i.kind == "cost_stance" and i.confirmed) or _newest(
        i for i in live if i.kind == "cost_stance"
    )
    if stance is None:
        return RANKS, None
    ranking = stance.parameters.get("ranking")
    order: tuple[str, ...] = RANKS
    if isinstance(ranking, list) and sorted(map(str, ranking)) == sorted(RANKS):
        order = tuple(map(str, ranking))
    slider = stance.parameters.get("slider")
    return order, float(slider) if isinstance(slider, int | float) else None


def _in_span(now: time, start: time, end: time) -> bool:
    if start == end:
        return True
    if start < end:
        return start <= now < end
    return now >= start or now < end


def _in_season(day: date, season: tuple[str, str]) -> bool:
    md = f"{day.month:02d}-{day.day:02d}"
    first, last = season
    return first <= md <= last if first <= last else md >= first or md <= last
