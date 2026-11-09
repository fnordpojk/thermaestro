"""The tank's learning, day by day, from what is recorded.

Once a day is over (and an hour more, so a charge or draw across midnight is seen), it is
read: its charges and draws are kept as events, the week's demand is judged against it
and then learns from it, and the standing loss, the ceiling and the proofs are worked out
again. Beside the week, what is drawn by kind of day (home, away, a departure, a return) is
judged (`learn.drawn`), and planned with instead once it has beaten it. Days still in the
raw history (two weeks) that were never read are read in order, so a new install learns
from what it has recorded so far.

A tank is learned only once its top, its charge sensor and the pump's demand have each
behaved as they should (the input checks).
"""

import logging
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import date, datetime, timedelta, tzinfo
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from .. import clock
from ..core.failures import failures
from ..core.host import PluginHost
from ..core.values import Key, Values
from ..intents.calendar import Calendar
from ..store import Climate, Database, Home, Location, Transaction
from . import drawn, occupied, records, tank, water
from .model import Learned, State, mark
from .records import (
    CHARGE,
    DAY_KIND,
    DRAW,
    KEEP_S,
    OFFER_DISMISSED,
    TANK_DAY,
    ChargeSeen,
    DayKind,
    DrawSeen,
    Gap,
    OfferDismissed,
    TankDay,
)
from .store import Learnings
from .validate import held_out, unjudged

if TYPE_CHECKING:
    from ..intents import Intents

log = logging.getLogger(__name__)
LEARNING_TANK = mark("Learning a hot-water tank")

LOSS = "tank.loss"
DEMAND = "tank.demand"
DRAWS = "tank.draws"
"""What will be drawn by kind of day (`learn.drawn`): planned with instead of the week once
it has beaten it."""
CEILING = "tank.ceiling"
JUDGED_DAYS = 14
"""The days the demand's proof looks back over."""
LOSS_DAYS = 13
LOSS_TEST_S = 2 * 86_400.0
"""The standing loss is judged on the last two days, fitted on the days before."""

NOT_USABLE = mark("Not learned: %(what)s isn't usable on this pump yet.")
LEARNING = unjudged(mark("Days learned from: %(days)s. Judged once each weekday has been seen."))
LOSS_LEARNING = unjudged(mark("Judged once there are two days to test it on."))
CEILING_WHY = mark(
    "The highest the top has reached at the end of a charge, the periodic increase left out."
)
WHAT = {
    "top": mark("the tank's top temperature"),
    "low": mark("the tank's charge sensor"),
    "demand": mark("the pump's demand"),
}


@dataclass(frozen=True, slots=True)
class TankKeys:
    scope: str
    """`instance:path` of the tank."""
    top: Key
    low: Key
    demand: Key
    stop: Key
    periodic: Key | None = None
    """The periodic increase's own stop temperature, where the pump has one."""


class HotWater:
    def __init__(
        self,
        db: Database,
        values: Values,
        host: PluginHost,
        learnings: Learnings,
        usable: Callable[[Key], bool],
        intents: "Intents | None" = None,
    ) -> None:
        self._db = db
        self._values = values
        self._host = host
        self._learnings = learnings
        self._usable = usable
        self._intents = intents

    def tanks(self) -> Iterator[TankKeys]:
        for instance_id, instance in sorted(self._host.instances.items()):
            described = instance.described
            if described is None:
                continue
            units = [n.path for n in described.nodes if n.kind == "unit"]
            for node in described.nodes:
                if node.kind != "dhw_tank":
                    continue
                unit = next((u for u in units if node.path.startswith(f"{u}/")), None)
                if unit is None:
                    continue
                yield TankKeys(
                    f"{instance_id}:{node.path}",
                    Key(instance_id, f"{node.path}/temp.top"),
                    Key(instance_id, f"{node.path}/temp.charge"),
                    Key(instance_id, f"{unit}/demand"),
                    Key(instance_id, f"{node.path}/temp.stop"),
                    Key(instance_id, f"{node.path}/temp.stop.periodic"),
                )

    async def run(self, now: float | None = None) -> None:
        now = clock.time() if now is None else now
        calendar, season = await house(self._db)
        for keys in self.tanks():
            part = f"learn.tank:{keys.scope}"
            try:
                await self._learn(keys, calendar, season, now)
            except Exception as e:  # noqa: BLE001 - logged and raised as an event
                failures.failed(part, LEARNING_TANK, e, where=keys.scope, logger=log)
            else:
                failures.worked(part)

    async def _learn(
        self, keys: TankKeys, calendar: Calendar, season: Callable[[date], float], now: float
    ) -> None:
        missing = [
            WHAT[what]
            for what, key in (("top", keys.top), ("low", keys.low), ("demand", keys.demand))
            if not self._usable(key)
        ]
        if missing:
            await self._note(keys.scope, missing[0])
            return
        zone = calendar.zone
        first = await self._first_raw(keys.top)
        if first is None:
            return
        day = tank.first_day(first, await self._last_day(keys.scope), zone)
        end = now - 3600
        while tank.bounds(day, zone)[1] <= end:
            await self._read(keys, day, calendar, season)
            day += timedelta(days=1)
        await self._judge(keys, now)
        await self._by_kind(keys, calendar, now)

    async def _by_kind(self, keys: TankKeys, calendar: Calendar, now: float) -> None:
        """Each day's kind as known now, kept for the planner; and the line by kind of day
        judged as at each midnight of the days before (`learn.drawn`)."""
        zone = calendar.zone
        since = now - (drawn.POOL_DAYS + drawn.JUDGED_DAYS + 2) * 86_400
        found = [d for _, d in await self.days(keys.scope, since) if isinstance(d, TankDay)]
        if not found:
            return
        read = {d.day: tuple(d.hourly) for d in found}
        predicted = {d.day: (d.predicted, d.baseline) for d in found}
        tanks = [k.scope for k in self.tanks()]
        rows = await occupied.history(self._db, since, now, tanks, zone, self._intents)
        known = occupied.line(rows, now)

        def free(day: date) -> bool:
            return calendar.weekday(day) >= 5

        kinds = {d: known.day(d, zone) for d in read}
        today = datetime.fromtimestamp(now, zone).date()
        kept = [
            records.row(DAY_KIND, tank.bounds(d, zone)[0], DayKind(day=d, kind=k, free=free(d)))
            for d, k in kinds.items()
            if d >= today - timedelta(days=drawn.POOL_DAYS)
        ]
        await self._db.run(lambda t: records.keep(t, keys.scope, kept))
        moments = []
        for back in range(drawn.JUDGED_DAYS, 1, -1):
            day = today - timedelta(days=back)
            if day in read and day + timedelta(days=1) in read:
                moments.append(judged_at(day, kinds[day], read, predicted, rows, zone, free))
        scored = drawn.score(moments)
        await self._learnings.put(
            Learned(
                name=DRAWS,
                scope=keys.scope,
                kind="model",
                value={"by_class": 1.0 if scored.by_class else 0.0},
                state="proved" if scored.proved else "learning",
                why=scored.why,
                args=scored.args,
                n=len(moments),
                since=tank.bounds(found[0].day, zone)[0],
            )
        )

    async def _read(
        self, keys: TankKeys, day: date, calendar: Calendar, season: Callable[[date], float]
    ) -> None:
        zone = calendar.zone
        start, end = tank.bounds(day, zone)
        since = end - LOSS_DAYS * 86_400
        top = tank.Series(await self._values.history(keys.top, since, end + 3600))
        low = tank.Series(await self._values.history(keys.low, since, end + 3600))
        if not (tank.covers(top.samples, start, end) and tank.covers(low.samples, start, end)):
            # Thermaestro or the pump's connection was down: what was drawn isn't known,
            # which isn't the same as nothing drawn. Marked read, learned from nothing.
            gap = [records.row(TANK_DAY, start, Gap(day=day, skipped="gap"))]
            await self._db.run(lambda t: records.keep(t, keys.scope, gap))
            return
        demand = await self._values.history(keys.demand, since - 3600, end + 3600)
        found = tank.charges(demand, end + 3600, top)
        hours = tank.steps(top, low, found, since, end, step=3600)
        k = tank.loss_rate(hours) or 0.0
        quarters = tank.steps(top, low, found, since, end)
        opens = tank.threshold(quarters, k)
        scale = season(day)
        read = tank.read_day(day, zone, top, low, found, k, opens, scale)
        learned = self._learnings.get(DEMAND, keys.scope)
        demand_now = tank.Demand.from_value(learned.value if learned else None)
        weekday = calendar.weekday(day)
        if demand_now.seen[weekday]:
            predicted: list[float] | None = list(demand_now.day(weekday))
            flat = sum(demand_now.hours) / tank.HOURS
            baseline: list[float] | None = [flat] * 24
        else:
            predicted = baseline = None
        demand_now.learn(weekday, read.hourly)
        n = (learned.n if learned else 0) + 1
        await self._learnings.put(
            # Not judged yet: a week proved before stays in use until the judge proves this.
            Learned(
                name=DEMAND,
                scope=keys.scope,
                kind="model",
                value=demand_now.to_value(),
                unit="tank_c",
                state="learning",
                why=LEARNING,
                args={"days": n},
                n=n,
                since=learned.since if learned and learned.since else start,
            )
        )
        seen = TankDay(
            day=day,
            hourly=list(read.hourly),
            predicted=predicted,
            baseline=baseline,
            draws=len(read.draws),
            drawn=round(sum(d.size for d in read.draws), 3),
            charges=len(read.charges),
            k=k,
            opens=opens,
            season=round(scale, 3),
        )
        rows = [
            records.row(TANK_DAY, start, seen),
            *(
                records.row(DRAW, d.at, DrawSeen(start=d.start, end=d.end, size=d.size))
                for d in read.draws
            ),
            *(
                records.row(CHARGE, c.start, ChargeSeen(end=c.end, top0=c.top0, top1=c.top1))
                for c in read.charges
            ),
        ]
        await self._db.run(lambda t: records.keep(t, keys.scope, rows))

    async def _judge(self, keys: TankKeys, now: float) -> None:
        """The demand's proof from the days kept; the standing loss fitted and judged; the
        ceiling."""
        days = await self.days(keys.scope, now - JUDGED_DAYS * 86_400)
        judged = [
            (d.predicted, d.baseline, d.hourly)
            for _, d in days
            if isinstance(d, TankDay) and d.predicted is not None and d.baseline is not None
        ]
        learned = self._learnings.get(DEMAND, keys.scope)
        if learned is not None and learned.value is not None:
            if judged:
                verdict = tank.judge(judged)
                state = "proved" if verdict.proved else "learning"
                why, args = verdict.why, verdict.args
            else:
                state, why, args = "learning", LEARNING, {"days": learned.n}
            update = {"state": state, "why": why, "args": args}
            await self._learnings.put(learned.model_copy(update=update))
        await self._loss(keys, now)
        await self._ceiling(keys, now)

    async def _loss(self, keys: TankKeys, now: float) -> None:
        since = now - LOSS_DAYS * 86_400
        top = tank.Series(await self._values.history(keys.top, since, now))
        low = tank.Series(await self._values.history(keys.low, since, now))
        demand = await self._values.history(keys.demand, since - 3600, now)
        found = tank.charges(demand, now, top)
        hours = tank.steps(top, low, found, since, now, step=3600)
        split = now - LOSS_TEST_S
        train = [h for h in hours if h[1] <= split]
        test = [h for h in hours if h[0] >= split]
        k_all = tank.loss_rate(hours)
        if k_all is None:
            return
        k_train = tank.loss_rate(train)
        state: State = "learning"
        why: str | None = LOSS_LEARNING
        args: dict[str, float | int | str] = {}
        if k_train is not None and test:
            flat = sorted(h[2] - h[3] for h in train)
            guess = flat[len(flat) // 2] if flat else 0.0
            # Quiet hours only: a draw is no standing loss, and is left out of the test.
            quiet = [h for h in test if (h[2] - h[3]) <= 3 * max(guess, 0.1)]
            predicted = [k_train * ((h[2] + h[3]) / 2 - tank.ROOM_C) for h in quiet]
            actual = [h[2] - h[3] for h in quiet]
            verdict = held_out(predicted, [guess] * len(quiet), actual)
            state = "proved" if verdict.proved else "learning"
            why, args = verdict.why, verdict.args
        before = self._learnings.get(LOSS, keys.scope)
        await self._learnings.put(
            Learned(
                name=LOSS,
                scope=keys.scope,
                kind="model",
                value=round(k_all, 5),
                unit="1/h",
                state=state,
                why=why,
                args=args,
                n=len(hours),
                since=before.since if before and before.since else since,
            )
        )

    async def _ceiling(self, keys: TankKeys, now: float) -> None:
        # The periodic increase is told by its own stop, where the pump has one: the mode's
        # stop changes with the mode, so a charge in a warmer mode would look like one.
        periodic_c = await self._stop(keys.periodic) if keys.periodic is not None else None
        bar = periodic_c if periodic_c is not None else await self._stop(keys.stop)
        found = await records.read(self._db, keys.scope, CHARGE, now - KEEP_S)
        ends = [
            c.top1
            for _, c in found
            if c.top1 is not None and (bar is None or c.top1 < bar + tank.PERIODIC_ABOVE)
        ]
        if not ends:
            return
        before = self._learnings.get(CEILING, keys.scope)
        await self._learnings.put(
            Learned(
                name=CEILING,
                scope=keys.scope,
                kind="observed",
                value=max(ends),
                unit="°C",
                state="learning",
                why=CEILING_WHY,
                n=len(ends),
                since=before.since if before and before.since else found[0][0],
            )
        )

    async def _stop(self, key: Key) -> float | None:
        """The tank's stop temperature: as read now, or, just after a restart before it is
        read again, the last one kept."""
        stop = self._values.latest.get(key)
        if stop is not None and isinstance(stop.value, int | float):
            return float(stop.value)
        kept = (await self._values.last([key])).get(key)
        if kept is not None and kept.quality == "good" and kept.value is not None:
            return kept.value
        return None

    async def _note(self, scope: str, what: str) -> None:
        """Why the tank isn't learned, where nothing has been."""
        found = self._learnings.get(DEMAND, scope)
        if found is not None and found.state != "starting":
            return
        await self._learnings.put(
            Learned(
                name=DEMAND,
                scope=scope,
                kind="model",
                state="starting",
                why=NOT_USABLE,
                args={"what": what},
            )
        )

    async def _first_raw(self, key: Key) -> float | None:
        def read(t: Transaction) -> float | None:
            row = t.execute(
                "SELECT MIN(t) FROM history WHERE instance = ? AND point = ?",
                (key.instance, key.point),
            ).fetchone()
            return None if row is None else row[0]

        return await self._db.run(read)

    async def _last_day(self, scope: str) -> date | None:
        found = await records.last(self._db, scope, TANK_DAY)
        return found[1].day if found else None

    async def days(self, scope: str, since: float) -> list[tuple[float, TankDay | Gap]]:
        """The tank's days from `since` on: each read, or passed over for a gap."""
        return await records.read(self._db, scope, TANK_DAY, since)

    async def dismiss(self, scope: str, offer: str) -> None:
        """An offer the household turned down."""
        kept = [records.row(OFFER_DISMISSED, clock.time(), OfferDismissed(id=offer))]
        await self._db.run(lambda t: records.keep(t, scope, kept))

    async def dismissed(self, scope: str, since: float) -> set[str]:
        """The offers turned down since `since`."""
        return {d.id for _, d in await records.read(self._db, scope, OFFER_DISMISSED, since)}


def expected(
    learnings: Learnings,
    scope: str,
    calendar: Calendar,
    season: Callable[[date], float],
    start: datetime,
    hours: int = 48,
) -> tuple[float, tuple[float, ...]] | None:
    """A tank's standing loss and the draws expected hour by hour from `start`, where both
    are to be used: proved, or the loss pinned."""
    loss = learnings.used(LOSS, scope)
    demand = learnings.used(DEMAND, scope)
    if not isinstance(loss, int | float) or not isinstance(demand, dict):
        return None
    week = tank.Demand.from_value(demand)
    drawn = tank.expected(week, calendar.weekday, calendar.zone, season, start, hours)
    return float(loss), drawn


def judged_at(
    day: date,
    kind: "occupied.DayLabel",
    read: dict[date, tuple[float, ...]],
    predicted: dict[date, tuple[list[float] | None, list[float] | None]],
    rows: list[occupied.Row],
    zone: tzinfo,
    free: Callable[[date], bool],
) -> drawn.Moment:
    """`day`'s midnight judged: each guess's line then for it and the day after, the line by
    kind of day from the days before as they were labeled then, with whether anyone was home
    as concluded by then; `kind`, the day's own as known now, for the report."""
    after = day + timedelta(days=1)
    midnight = tank.bounds(day, zone)[0]
    then = occupied.line(rows, midnight)
    past = [
        drawn.Past(d, read[d], then.day(d, zone), free(d))
        for d in read
        if day - timedelta(days=drawn.POOL_DAYS) <= d < day
    ]
    expected: occupied.DayLabel = "away" if then.at(midnight) == "away" else "home"
    ahead = {d: drawn.Ahead(d, expected, free(d)) for d in (day, after)}
    hours = [(d, h) for d in (day, after) for h in range(24)]
    guesses = drawn.others(day, read, predicted)
    for model, by_class in (("plain", False), ("by_class", True)):
        found = drawn.line(hours, ahead, past, by_class)
        if found is not None:
            guesses[model] = found
    actual = drawn.blocks([*read[day], *read[after]])
    return drawn.Moment(tuple(actual), kind, guesses)


async def planned(
    db: Database,
    learnings: Learnings,
    scope: str,
    calendar: Calendar,
    season: Callable[[date], float],
    start: datetime,
    now: str | None = None,
    away: Callable[[datetime], bool] = lambda at: False,
) -> tuple[float, tuple[float, ...]] | None:
    """A tank's standing loss and the draws to plan for, hour by hour from `start`: by kind
    of day once that has beaten the week (`learn.drawn`), with whether anyone is home as last
    concluded (`now`) and an away request's hours (`away`); else the week, as `expected`."""
    loss = learnings.used(LOSS, scope)
    model = learnings.used(DRAWS, scope)
    if isinstance(loss, int | float) and isinstance(model, dict):
        zone = calendar.zone
        hours = drawn.local_hours(start.timestamp(), zone)
        since = start.timestamp() - (drawn.POOL_DAYS + 1) * 86_400
        read = {
            d.day: tuple(d.hourly)
            for _, d in await records.read(db, scope, TANK_DAY, since)
            if isinstance(d, TankDay)
        }
        kinds = {k.day: k for _, k in await records.read(db, scope, DAY_KIND, since)}
        past = [
            drawn.Past(d, hourly, kinds[d].kind, kinds[d].free)
            for d, hourly in read.items()
            if d in kinds
        ]
        days = sorted({d for d, _ in hours})
        ahead = {
            d: drawn.Ahead(d, kind, free, season(d))
            for d, (kind, free) in drawn.kinds_ahead(
                days, zone, now, away, lambda d: calendar.weekday(d) >= 5
            ).items()
        }
        found = drawn.line(hours, ahead, past, bool(model.get("by_class")))
        if found is not None:
            return float(loss), drawn.hourly(found)
    return expected(learnings, scope, calendar, season, start)


async def house(db: Database) -> tuple[Calendar, Callable[[date], float]]:
    """The house's days (its zone, its holidays) and how much more heat a draw takes on a
    day than at the year's mean cold water: 1 where the climate isn't known."""
    location = await db.get(Location)
    home = await db.get(Home) or Home()
    climate = await db.get(Climate)
    zone = ZoneInfo(location.timezone) if location else ZoneInfo("UTC")
    calendar = Calendar(zone, home.holidays, home.holidays_as)
    if climate is None:
        return calendar, lambda day: 1.0
    well, south = home.water == "well", location is not None and location.latitude < 0
    mean, spread = climate.annual_mean, climate.monthly_spread
    return calendar, lambda day: water.season(day, mean, spread, well=well, south=south)
