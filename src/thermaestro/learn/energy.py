"""The compressor's electricity, learned day by day from what is recorded.

Once a day is over (and an hour more), the house's power steps at the compressor's starts
and stops are read and kept as events, with the day's starts and how long the compressor
ran for each purpose. Where the pump has a meter of its own on its supply, the draw over
each stretch it ran on one purpose is read from that too, with the day's energy and how
much of it was used while the addition ran. Days still in the raw history (two weeks)
are read in order. The draw is the median of the last four weeks' stretches from the
pump's meter, where there are enough, else of the house's steps; judged on the last two
days.

It is learned only once the compressor's state, the pump's demand and a power reading
(the pump's meter, or the house's) have each behaved as they should (the input checks).
"""

import json
import logging
from collections.abc import Callable, Iterator
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from functools import partial
from typing import Any

from .. import clock
from ..core.host import PluginHost
from ..core.values import Key, Values
from ..store import Database, Transaction
from . import electric, tank
from .hotwater import KEEP_S, house
from .inputs import intersect, spans
from .meters import pump_meters
from .model import Learned, State, mark
from .store import Learnings

log = logging.getLogger(__name__)

DRAW = "compressor.draw"
STARTS = "compressor.starts"
PUMP_ENERGY = "pump.energy"
STEP_DAYS = 28
"""The days of steps the draw is the median of."""
STARTS_DAYS = 7

NOT_USABLE = mark("Not learned: %(what)s isn't usable on this pump yet.")
STARTS_WHY = mark("The mean over the last %(days)s days.")
ENERGY_WHY = mark(
    "The mean over the last %(days)s days, from the electricity meter on the pump's supply;"
    " %(addition)s kWh a day of it while the addition ran."
)
WHAT = {
    "house": mark(
        "a power reading: an electricity meter on the pump's supply, or the whole house's"
    ),
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
    meter: Key | None = None
    """An electricity meter on the pump's supply, in kWh, where one is set up."""


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
        meters = await pump_meters(self._db)
        for keys in self.units():
            keys = replace(keys, meter=meters.get(keys.scope))
            try:
                await self._learn(keys, found, calendar.zone, now)
            except Exception:
                log.exception(
                    "learning the compressor of %s failed; trying again later", keys.scope
                )

    async def _learn(
        self, keys: UnitKeys, found: tuple[Key, float] | None, zone: Any, now: float
    ) -> None:
        house_ = found if found is not None and self._usable(found[0]) else None
        meter = keys.meter if keys.meter is not None and self._usable(keys.meter) else None
        missing = (
            "compressor"
            if keys.compressor is None or not self._usable(keys.compressor)
            else "demand"
            if not self._usable(keys.demand)
            else "house"
            if house_ is None and meter is None
            else None
        )
        if missing is not None or keys.compressor is None:
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
            await self._read(keys, keys.compressor, house_, meter, day, zone)
            day += timedelta(days=1)
            read = True
        if read or self._learnings.get(DRAW, keys.scope) is None:
            await self._judge(keys.scope, now)

    async def _read(
        self,
        keys: UnitKeys,
        state: Key,
        found: tuple[Key, float] | None,
        meter: Key | None,
        day: date,
        zone: Any,
    ) -> None:
        start, end = tank.bounds(day, zone)
        since, until = start - 3600, end + 3600
        compressor = await self._values.history(state, since, until)
        demand = await self._values.history(keys.demand, since, until)
        addition: list[tuple[float, float]] = []
        if keys.addition is not None and self._usable(keys.addition):
            samples = await self._values.history(keys.addition, since, until)
            addition = spans(samples, until, lambda s: bool(s.value))
        ran = electric.runs(compressor, until)
        seen: list[electric.Step] = []
        if found is not None:
            power = tank.Series(await self._values.history(found[0], since, until))
            seen = electric.steps(
                ran, power, demand, factor=found[1], addition=addition, start=start, end=end
            )
        body: dict[str, Any] = {
            "day": day.isoformat(),
            "starts": electric.starts(ran, start, end),
            "run_s": electric.run_seconds(ran, demand, start, end),
            "steps": len(seen),
        }
        stretches: list[electric.Step] = []
        if meter is not None:
            counter = tank.Series(await self._values.history(meter, since, until))
            stood = spans(compressor, until, lambda s: s.text == "stopped")
            standby = electric.standby_kw(stood, counter) or 0.0
            serving = {
                p: intersect(ran, spans(demand, until, partial(_serves, p)))
                for p in electric.PURPOSES
            }
            stretches = electric.meter_runs(
                serving, counter, standby, addition=addition, start=start, end=end
            )
            kwh = electric.counter_kwh(counter, start, end)
            if kwh is not None:
                body["kwh"] = round(kwh, 3)
                during = [
                    electric.counter_kwh(counter, max(a0, start), min(a1, end))
                    for a0, a1 in addition
                    if a0 < end and start < a1
                ]
                body["addition_kwh"] = round(sum(k for k in during if k is not None), 3)
            body["standby_kw"] = round(standby, 3)
        rows: list[tuple[str, float, dict[str, Any]]] = [
            ("compressor_day", start, body),
            *(
                ("power_step", s.t, {"edge": s.edge, "purpose": s.purpose, "kw": s.kw})
                for s in seen
            ),
            *(("power_run", s.t, {"purpose": s.purpose, "kw": s.kw}) for s in stretches),
        ]
        await self._db.run(lambda t: _events(t, keys.scope, rows))

    async def _judge(self, scope: str, now: float) -> None:
        since_ = now - STEP_DAYS * 86_400
        steps = [
            electric.Step(t, b["edge"], b["purpose"], b["kw"])
            for t, b in await self._events(scope, "power_step", since_)
        ]
        metered = [
            electric.Step(t, "run", b["purpose"], b["kw"])
            for t, b in await self._events(scope, "power_run", since_)
        ]
        found = metered if electric.draws(metered) else steps
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
        used = [b for _, b in recent if "kwh" in b]
        if used:
            addition = sum(b.get("addition_kwh", 0.0) for b in used) / len(used)
            await self._learnings.put(
                Learned(
                    name=PUMP_ENERGY,
                    scope=scope,
                    kind="observed",
                    value=round(sum(b["kwh"] for b in used) / len(used), 1),
                    unit="kWh/day",
                    state="learning",
                    why=ENERGY_WHY,
                    args={"days": len(used), "addition": round(addition, 1)},
                    n=len(used),
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


def _serves(purpose: str, s: Any) -> bool:
    return bool(s.text == purpose)


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
