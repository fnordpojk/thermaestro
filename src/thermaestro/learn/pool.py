"""A pool, learned day by day: how fast the compressor warms it, and how fast it cools.

Once a day is over (and an hour more), its hours are read and kept as events: the pool's
mean temperature, the outdoor temperature, and the share of the hour the compressor ran
with the demand on the pool. From one hour to the next

    pool' - pool = a · share - b · (pool - outdoor)

fitted by least squares over the last four weeks. Judged on the last four days, each
hour's next temperature against the pool simply staying where it was. A pool is learned
once its temperature, the demand, the compressor and an outdoor temperature are usable.
"""

import itertools
import json
import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from functools import partial
from typing import Any

from .. import clock
from ..core.host import PluginHost
from ..core.sensors import SITE
from ..core.values import Key, Values
from ..store import Database, Transaction
from .hotwater import house as house_days
from .hygiene import intervals
from .inputs import intersect, seconds, spans
from .model import Learned, State, mark
from .pump import _solve
from .store import Learnings
from .tank import bounds, first_day
from .validate import held_out

log = logging.getLogger(__name__)

POOL = "pool.model"
DAYS = 28
LEAST = 48
"""Hour pairs a fit needs."""
HEATED_LEAST = 6
"""Hours the compressor heated the pool, at least, among them."""
TEST_HOURS = 96

FEW = mark("%(hours)s hours of %(least)s to learn the pool from.")
UNHEATED = mark("The pool hasn't been heated enough to learn how fast it warms.")
MISSING = mark("Not learned: %(what)s isn't usable on this pump yet.")
WHAT = {
    "temp": mark("the pool's temperature"),
    "running": mark("the compressor's state or the demand"),
    "outdoor": mark("an outdoor temperature"),
}


@dataclass(frozen=True, slots=True)
class Hour:
    t: float
    pool: float | None
    outdoor: float | None
    share: float | None


@dataclass(frozen=True, slots=True)
class Fit:
    a: float
    """K an hour of the compressor's full heating gives."""
    b: float
    """The share of the pool's lead over the outdoor temperature it loses an hour."""
    rows: int

    def next(self, pool: float, outdoor: float, share: float) -> float:
        return pool + self.a * share - self.b * (pool - outdoor)

    def as_value(self) -> dict[str, float]:
        return {"a": round(self.a, 4), "b": round(self.b, 5)}


def _rows(hours: Sequence[Hour]) -> list[tuple[Hour, float]]:
    out = []
    for h, after in itertools.pairwise(hours):
        if after.t - h.t != 3600 or h.pool is None or after.pool is None:
            continue
        if h.outdoor is None or h.share is None:
            continue
        out.append((h, after.pool))
    return out


def fit(hours: Sequence[Hour]) -> tuple[Fit | None, str | None, dict[str, float | int | str]]:
    rows = _rows(hours)
    if len(rows) < LEAST:
        return None, FEW, {"hours": len(rows), "least": LEAST}
    if sum(1 for h, _ in rows if (h.share or 0) > 0.2) < HEATED_LEAST:
        return None, UNHEATED, {}
    found = _solve(
        [
            ([h.share or 0.0, -((h.pool or 0.0) - (h.outdoor or 0.0))], after - (h.pool or 0.0))
            for h, after in rows
        ]
    )
    if found is None or found[0] <= 0 or found[1] < 0:
        return None, UNHEATED, {}
    return Fit(found[0], found[1], len(rows)), None, {}


def judge(
    hours: Sequence[Hour], test_from: float
) -> tuple[Fit | None, Any, str | None, dict[str, float | int | str]]:
    train = [h for h in hours if h.t < test_from]
    trained, why, args = fit(train)
    verdict = None
    if trained is not None:
        test = [(h, a) for h, a in _rows(hours) if h.t >= test_from]
        verdict = held_out(
            [trained.next(h.pool or 0.0, h.outdoor or 0.0, h.share or 0.0) for h, _ in test],
            [h.pool for h, _ in test],
            [a for _, a in test],
        )
    whole, why_all, args_all = fit(hours)
    if whole is None:
        return None, verdict, why_all or why, args_all or args
    return whole, verdict, None, {}


@dataclass(frozen=True, slots=True)
class PoolKeys:
    scope: str
    temp: Key
    demand: Key
    compressor: Key | None
    outdoor: list[Key]


class Pools:
    def __init__(
        self,
        db: Database,
        values: Values,
        host: PluginHost,
        learnings: Learnings,
        usable: Callable[[Key], bool],
    ) -> None:
        self._db = db
        self._values = values
        self._host = host
        self._learnings = learnings
        self._usable = usable

    def pools(self) -> list[PoolKeys]:
        out = []
        for instance_id, instance in sorted(self._host.instances.items()):
            described = instance.described
            if described is None:
                continue
            kinds = {n.path: n.kind for n in described.nodes}
            paths = {p.path for p in described.points}
            for node in described.nodes:
                if node.kind != "pool" or f"{node.path}/temp" not in paths:
                    continue
                unit = node.path.split("/")[0]
                compressor = next(
                    (
                        Key(instance_id, p)
                        for p in sorted(paths)
                        if p.startswith(f"{unit}/")
                        and p.endswith("/state")
                        and kinds.get(p.rpartition("/")[0]) == "compressor"
                    ),
                    None,
                )
                out.append(
                    PoolKeys(
                        f"{instance_id}:{node.path}",
                        Key(instance_id, f"{node.path}/temp"),
                        Key(instance_id, f"{unit}/demand"),
                        compressor,
                        [
                            Key(SITE, "outdoor/temperature"),
                            Key(instance_id, f"{unit}/outdoor.temp"),
                        ],
                    )
                )
        return out

    async def run(self, now: float | None = None) -> None:
        now = clock.time() if now is None else now
        calendar, _ = await house_days(self._db)
        for keys in self.pools():
            try:
                await self._learn(keys, calendar.zone, now)
            except Exception:
                log.exception("learning the pool %s failed; trying again later", keys.scope)

    async def _learn(self, keys: PoolKeys, zone: Any, now: float) -> None:
        outdoor = next((k for k in keys.outdoor if self._usable(k)), None)
        if keys.compressor is None or not all(
            self._usable(k) for k in (keys.demand, keys.compressor)
        ):
            await self._note(keys.scope, WHAT["running"])
            return
        if not self._usable(keys.temp):
            await self._note(keys.scope, WHAT["temp"])
            return
        if outdoor is None:
            await self._note(keys.scope, WHAT["outdoor"])
            return
        first = await self._first_raw(keys.temp)
        if first is None:
            return
        day = first_day(first, await self._last_day(keys.scope), zone)
        read = False
        while bounds(day, zone)[1] <= now - 3600:
            hours = await self._day(keys, outdoor, keys.compressor, day, zone)
            body = {"day": day.isoformat(), "hours": [_body(h) for h in hours]}
            start = bounds(day, zone)[0]
            await self._db.run(partial(_keep, scope=keys.scope, start=start, body=body))
            day += timedelta(days=1)
            read = True
        if read or self._learnings.get(POOL, keys.scope) is None:
            await self._judge(keys.scope, now)

    async def _day(
        self, keys: PoolKeys, outdoor: Key, compressor: Key, day: date, zone: Any
    ) -> list[Hour]:
        start, end = bounds(day, zone)
        count = int((end - start) // 3600)

        async def hourly(key: Key, hold: float) -> list[float | None]:
            samples = await self._values.history(key, start - hold, end)
            found = intervals(samples, start, end, 3600, hold_s=hold)
            return [i.mean if i is not None else None for i in found]

        pool = await hourly(keys.temp, 3600.0)
        out = await hourly(outdoor, 3 * 3600.0)
        demand = await self._values.history(keys.demand, start - 3600, end)
        ran = await self._values.history(compressor, start - 3600, end)
        heated = intersect(
            spans(ran, end, lambda s: s.text == "running"),
            spans(demand, end, lambda s: s.text == "pool"),
        )
        shares = [
            seconds(intersect(heated, [(start + i * 3600, start + (i + 1) * 3600)])) / 3600
            for i in range(count)
        ]
        return [Hour(start + i * 3600, pool[i], out[i], shares[i]) for i in range(count)]

    async def _judge(self, scope: str, now: float) -> None:
        since = now - DAYS * 86_400

        def read(t: Transaction) -> list[dict[str, Any]]:
            rows = t.execute(
                "SELECT body FROM learn_events WHERE scope = ? AND kind = 'pool_day'"
                " AND t >= ? ORDER BY t",
                (scope, since),
            )
            return [json.loads(body) for (body,) in rows]

        hours = [_hour(h) for body in await self._db.run(read) for h in body["hours"]]
        if not hours:
            return
        whole, verdict, why, args = judge(hours, now - TEST_HOURS * 3600)
        before = self._learnings.get(POOL, scope)
        if whole is None:
            if before is not None and before.value is not None:
                return
            await self._learnings.put(
                Learned(name=POOL, scope=scope, kind="model", state="learning", why=why, args=args)
            )
            return
        state: State = "proved" if verdict is not None and verdict.proved else "learning"
        await self._learnings.put(
            Learned(
                name=POOL,
                scope=scope,
                kind="model",
                value=whole.as_value(),
                state=state,
                why=verdict.why if verdict is not None else None,
                args=dict(verdict.args) if verdict is not None else {},
                n=whole.rows,
                since=before.since if before and before.since else hours[0].t,
            )
        )

    async def _note(self, scope: str, what: str) -> None:
        found = self._learnings.get(POOL, scope)
        if found is not None and found.value is not None:
            return
        await self._learnings.put(
            Learned(
                name=POOL,
                scope=scope,
                kind="model",
                state="starting",
                why=MISSING,
                args={"what": what},
            )
        )

    async def _first_raw(self, key: Key) -> float | None:
        def read(t: Any) -> float | None:
            row = t.execute(
                "SELECT MIN(t) FROM history WHERE instance = ? AND point = ?",
                (key.instance, key.point),
            ).fetchone()
            return None if row is None else row[0]

        return await self._db.run(read)

    async def _last_day(self, scope: str) -> date | None:
        def read(t: Transaction) -> str | None:
            row = t.execute(
                "SELECT body FROM learn_events WHERE scope = ? AND kind = 'pool_day'"
                " ORDER BY t DESC LIMIT 1",
                (scope,),
            ).fetchone()
            return None if row is None else row[0]

        found = await self._db.run(read)
        return date.fromisoformat(json.loads(found)["day"]) if found else None


def _body(h: Hour) -> dict[str, Any]:
    return {
        "t": h.t,
        "pool": None if h.pool is None else round(h.pool, 3),
        "outdoor": None if h.outdoor is None else round(h.outdoor, 3),
        "share": None if h.share is None else round(h.share, 3),
    }


def _hour(body: dict[str, Any]) -> Hour:
    return Hour(float(body["t"]), body.get("pool"), body.get("outdoor"), body.get("share"))


def _keep(t: Transaction, scope: str, start: float, body: dict[str, Any]) -> None:
    t.execute(
        "INSERT OR REPLACE INTO learn_events (scope, kind, t, body) VALUES (?, 'pool_day', ?, ?)",
        (scope, start, json.dumps(body)),
    )
