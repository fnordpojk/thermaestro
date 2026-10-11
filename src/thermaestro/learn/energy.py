"""The compressor's electricity, learned day by day from what is recorded.

Once a day is over (and an hour more), the house's power steps at the compressor's starts
and stops are read and kept as events, with the day's starts and how long the compressor
ran for each purpose. Days still in the raw history (two weeks) are read in order. The
draw is the median of the last four weeks' steps, judged on the last two days.

It is learned only once the compressor's state, the pump's demand and the house's power
have each behaved as they should (the input checks).
"""

import json
import logging
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

from .. import clock
from ..core.host import PluginHost
from ..core.values import Key, Values
from ..store import Database, Transaction
from . import electric, tank
from .hotwater import KEEP_S, house
from .inputs import spans
from .model import Learned, State, mark
from .store import Learnings

log = logging.getLogger(__name__)

DRAW = "compressor.draw"
STARTS = "compressor.starts"
STEP_DAYS = 28
"""The days of steps the draw is the median of."""
STARTS_DAYS = 7

NOT_USABLE = mark("Not learned: %(what)s isn't usable on this pump yet.")
STARTS_WHY = mark("The mean over the last %(days)s days.")
WHAT = {
    "house": mark("a whole-house power reading"),
    "compressor": mark("the compressor's state"),
    "demand": mark("the pump's demand"),
}


@dataclass(frozen=True, slots=True)
class UnitKeys:
    scope: str
    """`instance:path` of the pump."""
    compressor: Key | None
    demand: Key
    addition: Key | None


class Energy:
    def __init__(
        self,
        db: Database,
        values: Values,
        host: PluginHost,
        learnings: Learnings,
        usable: Callable[[Key], bool],
        house_power: Callable[[], tuple[Key, float] | None],
    ) -> None:
        self._db = db
        self._values = values
        self._host = host
        self._learnings = learnings
        self._usable = usable
        self._house_power = house_power

    def units(self) -> Iterator[UnitKeys]:
        for instance_id, instance in sorted(self._host.instances.items()):
            described = instance.described
            if described is None:
                continue
            kinds = {n.path: n.kind for n in described.nodes}
            paths = sorted(p.path for p in described.points)
            for unit in (n.path for n in described.nodes if n.kind == "unit"):
                mine = [p for p in paths if p.startswith(f"{unit}/")]
                yield UnitKeys(
                    f"{instance_id}:{unit}",
                    next(
                        (
                            Key(instance_id, p)
                            for p in mine
                            if p.endswith("/state")
                            and kinds.get(p.rpartition("/")[0]) == "compressor"
                        ),
                        None,
                    ),
                    Key(instance_id, f"{unit}/demand"),
                    next(
                        (
                            Key(instance_id, p)
                            for p in mine
                            if p.endswith("/power")
                            and kinds.get(p.rpartition("/")[0]) == "addition"
                        ),
                        None,
                    ),
                )

    async def run(self, now: float | None = None) -> None:
        now = clock.time() if now is None else now
        calendar, _ = await house(self._db)
        found = self._house_power()
        for keys in self.units():
            try:
                await self._learn(keys, found, calendar.zone, now)
            except Exception:
                log.exception(
                    "learning the compressor of %s failed; trying again later", keys.scope
                )

    async def _learn(
        self, keys: UnitKeys, found: tuple[Key, float] | None, zone: Any, now: float
    ) -> None:
        missing = (
            "compressor"
            if keys.compressor is None or not self._usable(keys.compressor)
            else "demand"
            if not self._usable(keys.demand)
            else "house"
            if found is None or not self._usable(found[0])
            else None
        )
        if missing is not None or found is None or keys.compressor is None:
            await self._note(keys.scope, WHAT[missing or "house"])
            return
        first = await self._first_raw(keys.compressor)
        if first is None:
            return
        done = await self._last_day(keys.scope)
        day = (done + timedelta(days=1)) if done else datetime.fromtimestamp(first, zone).date()
        if tank.bounds(day, zone)[0] < first:
            day += timedelta(days=1)  # the first day recorded only in part
        read = False
        while tank.bounds(day, zone)[1] <= now - 3600:
            await self._read(keys, keys.compressor, found, day, zone)
            day += timedelta(days=1)
            read = True
        if read or self._learnings.get(DRAW, keys.scope) is None:
            await self._judge(keys.scope, now)

    async def _read(
        self, keys: UnitKeys, state: Key, found: tuple[Key, float], day: date, zone: Any
    ) -> None:
        start, end = tank.bounds(day, zone)
        since, until = start - 3600, end + 3600
        compressor = await self._values.history(state, since, until)
        demand = await self._values.history(keys.demand, since, until)
        power = tank.Series(await self._values.history(found[0], since, until))
        addition: list[tuple[float, float]] = []
        if keys.addition is not None and self._usable(keys.addition):
            samples = await self._values.history(keys.addition, since, until)
            addition = spans(samples, until, lambda s: bool(s.value))
        ran = electric.runs(compressor, until)
        seen = electric.steps(
            ran, power, demand, factor=found[1], addition=addition, start=start, end=end
        )
        rows: list[tuple[str, float, dict[str, Any]]] = [
            (
                "compressor_day",
                start,
                {
                    "day": day.isoformat(),
                    "starts": electric.starts(ran, start, end),
                    "run_s": electric.run_seconds(ran, demand, start, end),
                    "steps": len(seen),
                },
            ),
            *(
                ("power_step", s.t, {"edge": s.edge, "purpose": s.purpose, "kw": s.kw})
                for s in seen
            ),
        ]
        await self._db.run(lambda t: _events(t, keys.scope, rows))

    async def _judge(self, scope: str, now: float) -> None:
        kept = await self._events(scope, "power_step", now - STEP_DAYS * 86_400)
        found = [electric.Step(t, b["edge"], b["purpose"], b["kw"]) for t, b in kept]
        draws = electric.draws(found)
        before = self._learnings.get(DRAW, scope)
        since = before.since if before and before.since else (found[0].t if found else None)
        if draws:
            verdict = electric.judge(found, now - electric.TEST_S)
            state: State = "proved" if verdict.proved else "learning"
            await self._learnings.put(
                Learned(
                    name=DRAW,
                    scope=scope,
                    kind="model",
                    value=draws,
                    unit="kW",
                    state=state,
                    why=verdict.why,
                    args=dict(verdict.args),
                    n=len(found),
                    since=since,
                )
            )
        else:
            await self._learnings.put(
                Learned(
                    name=DRAW,
                    scope=scope,
                    kind="model",
                    unit="kW",
                    state="learning",
                    why=electric.FEW,
                    args={"steps": len(found)},
                    n=len(found),
                    since=since,
                )
            )
        days = await self._events(scope, "compressor_day", now - (STARTS_DAYS + 1) * 86_400)
        recent = days[-STARTS_DAYS:]
        if recent:
            mean = sum(b["starts"] for _, b in recent) / len(recent)
            await self._learnings.put(
                Learned(
                    name=STARTS,
                    scope=scope,
                    kind="observed",
                    value=round(mean, 1),
                    unit="/day",
                    state="learning",
                    why=STARTS_WHY,
                    args={"days": len(recent)},
                    n=len(recent),
                    since=recent[0][0],
                )
            )

    async def _note(self, scope: str, what: str) -> None:
        found = self._learnings.get(DRAW, scope)
        if found is not None and found.state != "starting":
            return
        await self._learnings.put(
            Learned(
                name=DRAW,
                scope=scope,
                kind="model",
                unit="kW",
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
        found = await self._events(scope, "compressor_day", 0.0)
        return date.fromisoformat(found[-1][1]["day"]) if found else None

    async def _events(
        self, scope: str, kind: str, since: float
    ) -> list[tuple[float, dict[str, Any]]]:
        def read(t: Transaction) -> list[tuple[float, dict[str, Any]]]:
            rows = t.execute(
                "SELECT t, body FROM learn_events WHERE scope = ? AND kind = ? AND t >= ?"
                " ORDER BY t",
                (scope, kind, since),
            )
            return [(at, json.loads(body)) for at, body in rows]

        return await self._db.run(read)


def charge_kw(learnings: Learnings) -> float | None:
    """What a hot-water charge draws, where it is learned and proved (or pinned) for a
    pump: the most of them, to be on the safe side under a power limit."""
    found = [
        used["dhw"]
        for x in learnings.all()
        if x.name == DRAW and isinstance(used := x.used, dict) and "dhw" in used
    ]
    return max(found) if found else None


def _events(t: Transaction, scope: str, rows: list[tuple[str, float, dict[str, Any]]]) -> None:
    t.executemany(
        "INSERT OR REPLACE INTO learn_events (scope, kind, t, body) VALUES (?, ?, ?, ?)",
        [(scope, kind, at, json.dumps(body)) for kind, at, body in rows],
    )
    t.execute("DELETE FROM learn_events WHERE t < ?", (clock.time() - KEEP_S,))
