"""How the learners did by condition, on a replay: each day judged with what they had learned
by the night before it, and grouped by whether anyone was home, as concluded by the end.

- **The house**, per climate system: from each hour of a day, six hours simulated with what
  was measured going in, by the house model and the one with the household's heat as each
  stood at the day's start, against what the rooms did; grouped by whether anyone was away
  in those six hours. The household's model is given who was home as concluded by the end:
  the report is of the house's physics, not of guessing who would be.
- **The tank**: at each midnight, what would be drawn by the end of each three hours of the
  day, by the week as it predicted that day and by the line by kind of day (`learn.drawn`)
  as it stood then, against what was drawn; grouped by the day's kind. Scored as the line is
  judged, by the quantile loss at nine in ten, with how often the draws stayed within the
  line beside it.

Both are given for every day each could be, proved or not: the report is of what each would
have made of the days, not of which the planner used.
"""

from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from datetime import date, datetime
from typing import TYPE_CHECKING, Any

from ..learn import drawn, house, occupied, records
from ..learn.heating import HOUSEHOLD, MODEL, _hour
from ..learn.hotwater import DRAWS, line_at
from ..learn.hotwater import house as house_days
from ..learn.records import TANK_DAY, TankDay
from ..learn.tank import bounds
from ..store import Database

if TYPE_CHECKING:
    from ..intents import Intents
    from ..learn import Learning
    from ..occupancy import Occupancy


@dataclass
class _Tally:
    model: list[float] = field(default_factory=list)
    household: list[float] = field(default_factory=list)
    """On the hours both predicted."""
    model_both: list[float] = field(default_factory=list)


@dataclass
class _Drawn:
    days: int = 0
    week: list[float] = field(default_factory=list)
    line: list[float] = field(default_factory=list)
    within: list[float] = field(default_factory=list)


def _mean(values: Sequence[float]) -> float | None:
    return round(sum(values) / len(values), 3) if values else None


class Conditions:
    def __init__(
        self,
        db: Database,
        learning: "Learning",
        intents: "Intents",
        occupancy: "Occupancy",
    ) -> None:
        self._db = db
        self._learning = learning
        self._intents = intents
        self._occupancy = occupancy
        self._house: dict[date, dict[str, tuple[house.Fit | None, house.Fit | None]]] = {}
        """Per day, per climate system: the house model and the household's as at its start."""
        self._tank: dict[date, dict[str, tuple[float, ...]]] = {}
        """Per day, per tank: the line by kind of day at its midnight, hour by hour."""

    async def before(self, day: date) -> None:
        """What was learned by `day`'s start, kept to judge it once it is over."""
        learnings = self._learning.learnings
        fits: dict[str, tuple[house.Fit | None, house.Fit | None]] = {}
        for keys in await self._learning.heating.systems():
            found = []
            for name in (MODEL, HOUSEHOLD):
                learned = learnings.get(name, keys.scope)
                value = learned.value if learned is not None else None
                found.append(house.Fit.from_value(value) if isinstance(value, dict) else None)
            fits[keys.scope] = (found[0], found[1])
        self._house[day] = fits
        calendar, _ = await house_days(self._db)
        midnight = datetime.fromtimestamp(bounds(day, calendar.zone)[0], calendar.zone)
        resolver = await self._intents.resolver()
        aways = [i for i in resolver.intents if i.kind == "away" and i.open]
        lines = {}
        for tank in self._learning.hot_water.tanks():
            model = learnings.used(DRAWS, tank.scope)
            by_class = isinstance(model, dict) and bool(model.get("by_class"))
            found_line = await line_at(
                self._db,
                tank.scope,
                calendar,
                lambda d: 1.0,  # as the days are kept: at the year's mean inlet
                midnight,
                self._occupancy.last_known,
                lambda at: any(resolver.live(a, at) for a in aways),
                by_class,
            )
            if found_line is not None:
                lines[tank.scope] = found_line[:24]
        self._tank[day] = lines

    async def report(self, now: float, since: float, days: Sequence[date]) -> dict[str, Any]:
        """Each learner by condition, over `days`."""
        calendar, _ = await house_days(self._db)
        zone = calendar.zone
        tanks = [k.scope for k in self._learning.hot_water.tanks()]
        seen = await occupied.timeline(self._db, now, since, tanks, zone, self._intents)
        kinds = {d: seen.day(d, zone) for d in days}
        return {
            "house": await self._houses(seen, days, since),
            "tank": await self._tanks(kinds, days, since),
        }

    async def _houses(
        self, seen: occupied.Timeline, days: Sequence[date], since: float
    ) -> dict[str, Any]:
        out: dict[str, Any] = {}
        calendar, _ = await house_days(self._db)
        zone = calendar.zone
        wanted = set(days)
        for keys in await self._learning.heating.systems():
            kept = await records.read(self._db, keys.scope, records.HOUSE_DAY, since - 86_400)
            back = self._learning.usable(keys.back)
            hours = sorted(
                (_hour(h, back) for _, day in kept for h in day.hours), key=lambda h: h.t
            )
            hours = [replace(h, home=seen.share(h.t)) for h in hours]
            known = house.labeled(hours)
            tallies: dict[str, _Tally] = {}
            for k, h in enumerate(hours):
                day = datetime.fromtimestamp(h.t, zone).date()
                if day not in wanted or k + house.AHEAD >= len(hours):
                    continue
                model, household = self._house.get(day, {}).get(keys.scope, (None, None))
                then = hours[k + house.AHEAD]
                if model is None or then.room is None or then.skip:
                    continue
                p = house.simulate(hours, k, model)
                if p is None:
                    continue
                window = known[k + 1 : k + house.AHEAD + 1]
                if any(x.home is None for x in window):
                    condition = "unknown"
                elif any(x.home is not None and x.home < 1 for x in window):
                    condition = "away"
                else:
                    condition = "home"
                tally = tallies.setdefault(condition, _Tally())
                tally.model.append(abs(p - then.room))
                q = house.simulate(known, k, household) if household is not None else None
                if q is not None:
                    tally.household.append(abs(q - then.room))
                    tally.model_both.append(abs(p - then.room))
            out[keys.scope] = {
                condition: {
                    "hours": len(t.model),
                    "model": _mean(t.model),
                    "both": len(t.household),
                    "model_both": _mean(t.model_both),
                    "household": _mean(t.household),
                }
                for condition, t in sorted(tallies.items())
            }
        return out

    async def _tanks(
        self, kinds: dict[date, occupied.DayLabel], days: Sequence[date], since: float
    ) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for keys in self._learning.hot_water.tanks():
            kept = {
                d.day: d
                for _, d in await records.read(self._db, keys.scope, TANK_DAY, since - 86_400)
                if isinstance(d, TankDay)
            }
            by: dict[str, _Drawn] = {}
            for day in days:
                found = kept.get(day)
                if found is None:
                    continue
                actual = drawn.blocks(found.hourly)
                tally = by.setdefault(kinds[day], _Drawn())
                tally.days += 1
                if found.predicted is not None:
                    week = drawn.blocks(found.predicted)
                    tally.week += [drawn.pinball(a, p) for a, p in zip(actual, week, strict=True)]
                line = self._tank.get(day, {}).get(keys.scope)
                if line is not None:
                    by_then = drawn.blocks(line)
                    for a, p in zip(actual, by_then, strict=True):
                        tally.line.append(drawn.pinball(a, p))
                        tally.within.append(1.0 if a <= p + 1e-9 else 0.0)
            out[keys.scope] = {
                kind: {
                    "days": t.days,
                    "week": _mean(t.week),
                    "line": _mean(t.line),
                    "within": _mean(t.within),
                }
                for kind, t in sorted(by.items())
            }
        return out
