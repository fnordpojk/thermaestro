"""The compressor's COP, learned day by day from what is recorded.

Once a day is over (and an hour more), its hot-water charges with no draw nearby and its
hours of heating are read: how long the compressor ran on each, the water it made, the
brine in and the brine's drop, and for a charge the tank's rise. They are kept as events,
so what is learned from them can be worked out again as the draw and the brine are.

The starting shares come from what the pump's plugin documents, or what the household
entered under House. A pump needs its tank's two temperatures, the demand, the compressor's
state and the brine's two temperatures to have passed their checks, and its draw to have
proved itself.
"""

import json
import logging
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

from .. import clock
from ..cap.model import Rating
from ..core.host import PluginHost
from ..core.values import Key, Sample, Values
from ..store import Database, Rated, Transaction
from . import cop, tank
from .energy import DRAW
from .hotwater import KEEP_S, LOSS, house
from .inputs import Span, intersect, spans
from .model import Learned, State
from .store import Learnings

log = logging.getLogger(__name__)

DHW = "cop.dhw"
HEATING = "cop.heating"
BRINE = "brine.w_k"
DAYS = 28
"""The days of charges and hours learned from."""
SAMPLE_S = 60.0


@dataclass(frozen=True, slots=True)
class PumpKeys:
    scope: str
    """`instance:path` of the pump."""
    tank: str | None
    compressor: Key
    demand: Key
    top: Key | None
    low: Key | None
    brine_in: Key | None
    brine_out: Key | None
    supply: Key | None
    liters: float | None
    """The tank's volume, as the plugin documents it."""
    rating: Rating | None
    """The compressor's documented figures."""
    compressor_ref: str


class Efficiency:
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

    def pumps(self) -> Iterator[PumpKeys]:
        for instance_id, instance in sorted(self._host.instances.items()):
            described = instance.described
            if described is None:
                continue
            nodes = {n.path: n for n in described.nodes}
            paths = {p.path for p in described.points}
            for unit in (n.path for n in described.nodes if n.kind == "unit"):
                compressors = [
                    c for c in _below(nodes, unit, "compressor") if f"{c}/state" in paths
                ]
                if not compressors:
                    continue
                tanks = _below(nodes, unit, "dhw_tank")
                brines = _below(nodes, unit, "brine_circuit")
                systems = _below(nodes, unit, "climate_system")
                tank_node = tanks[0] if tanks else None
                brine = brines[0] if brines else None
                tank_rating = nodes[tank_node].rating if tank_node else None
                yield PumpKeys(
                    scope=f"{instance_id}:{unit}",
                    tank=f"{instance_id}:{tank_node}" if tank_node else None,
                    compressor=Key(instance_id, f"{compressors[0]}/state"),
                    demand=Key(instance_id, f"{unit}/demand"),
                    top=_point(instance_id, paths, tank_node, "temp.top"),
                    low=_point(instance_id, paths, tank_node, "temp.charge"),
                    brine_in=_point(instance_id, paths, brine, "brine.in.temp"),
                    brine_out=_point(instance_id, paths, brine, "brine.out.temp"),
                    supply=_point(
                        instance_id, paths, systems[0] if systems else None, "supply.temp"
                    ),
                    liters=tank_rating.liters if tank_rating else None,
                    rating=nodes[compressors[0]].rating,
                    compressor_ref=f"{instance_id}:{compressors[0]}",
                )

    async def run(self, now: float | None = None) -> None:
        now = clock.time() if now is None else now
        calendar, _ = await house(self._db)
        rated = await self._db.get(Rated) or Rated()
        for keys in self.pumps():
            try:
                await self._learn(keys, rated, calendar.zone, now)
            except Exception:
                log.exception("learning the COP of %s failed; trying again later", keys.scope)

    def starting(self, keys: PumpKeys, rated: Rated) -> tuple[float | None, float | None]:
        """The documented shares of the ideal: heating's and hot water's."""
        if keys.rating is not None and keys.rating.heating:
            heating = cop.share([(p.source_c, p.supply_c, p.cop) for p in keys.rating.heating])
            dhw = cop.dhw_share(keys.rating.cop_dhw) if keys.rating.cop_dhw is not None else heating
            return heating, dhw
        asked = rated.cop.get(keys.compressor_ref)
        if asked is None:
            return None, None
        heating = cop.share([(asked.source_c, asked.supply_c, asked.cop)])
        return heating, heating

    def volume(self, keys: PumpKeys, rated: Rated) -> float | None:
        if keys.liters is not None:
            return keys.liters
        return rated.liters.get(keys.tank) if keys.tank else None

    async def _learn(self, keys: PumpKeys, rated: Rated, zone: Any, now: float) -> None:
        needed = [
            k
            for k in (
                keys.compressor,
                keys.demand,
                keys.top,
                keys.low,
                keys.brine_in,
                keys.brine_out,
            )
            if k is not None
        ]
        heating_start, dhw_start = self.starting(keys, rated)
        if len(needed) < 6 or not all(self._usable(k) for k in needed):
            await self._starting(keys.scope, heating_start, dhw_start)
            return
        first = await self._first_raw(keys.compressor)
        if first is None:
            return
        done = await self._last_day(keys.scope)
        day = (done + timedelta(days=1)) if done else datetime.fromtimestamp(first, zone).date()
        if tank.bounds(day, zone)[0] < first:
            day += timedelta(days=1)
        while tank.bounds(day, zone)[1] <= now - 3600:
            await self._read(keys, day, zone)
            day += timedelta(days=1)
        await self._judge(keys, rated, now, heating_start, dhw_start)

    async def _read(self, keys: PumpKeys, day: date, zone: Any) -> None:
        if not (keys.top and keys.low and keys.brine_in and keys.brine_out):
            return
        start, end = tank.bounds(day, zone)
        since, until = start - 3600, end + 3600

        async def history(key: Key) -> list[Sample]:
            return await self._values.history(key, since, until)

        top, low = tank.Series(await history(keys.top)), tank.Series(await history(keys.low))
        brine_in = tank.Series(await history(keys.brine_in))
        brine_out = tank.Series(await history(keys.brine_out))
        supply = tank.Series(await history(keys.supply)) if keys.supply else None
        demand = await history(keys.demand)
        ran = spans(await history(keys.compressor), until, lambda s: s.text == "running")
        on_dhw = intersect(ran, spans(demand, until, lambda s: s.text == "dhw"))
        on_heat = intersect(ran, spans(demand, until, lambda s: s.text == "heating"))
        draws = await self._draws(keys.tank, since, until) if keys.tank else []
        rows: list[tuple[str, float, dict[str, Any]]] = [
            ("cop_day", start, {"day": day.isoformat()})
        ]
        k = self._loss(keys.tank)
        for c in tank.charges(demand, until, top):
            if not start <= c.start < end:
                continue
            settled = c.end + tank.SETTLE_S
            if any(d0 < settled and c.start - tank.SETTLE_S < d1 for d0, d1 in draws):
                continue  # a draw would hide part of what the charge made
            before, after = tank._mean(top, low, c.start - 60), tank._mean(top, low, settled)
            stretch = intersect(on_dhw, [(c.start, c.end)])
            run = _run(c.start, stretch, low, brine_in, brine_out)
            if before is None or after is None or run is None:
                continue
            lost = k * ((before + after) / 2 - tank.ROOM_C) * (settled - c.start) / 3600
            rows.append(
                ("cop_charge", c.start, _body(run) | {"rise_c": round(after - before + lost, 3)})
            )
        if supply is not None:
            t = start
            while t < end:
                stretch = intersect(on_heat, [(t, t + 3600)])
                run = _run(t, stretch, supply, brine_in, brine_out)
                if run is not None and run.run_s >= cop.RUN_LEAST_S:
                    rows.append(("cop_hour", t, _body(run)))
                t += 3600
        await self._db.run(lambda tr: _events(tr, keys.scope, rows))

    async def _judge(
        self,
        keys: PumpKeys,
        rated: Rated,
        now: float,
        heating_start: float | None,
        dhw_start: float | None,
    ) -> None:
        draw = self._learnings.used(DRAW, keys.scope)
        liters = self.volume(keys, rated)
        charges = [
            _unbody(t, b)
            for t, b in await self._events(keys.scope, "cop_charge", now - DAYS * 86_400)
        ]
        hours = [
            _unbody(t, b)
            for t, b in await self._events(keys.scope, "cop_hour", now - DAYS * 86_400)
        ]
        dhw_kw = draw.get("dhw") if isinstance(draw, dict) else None
        heat_kw = draw.get("heating") if isinstance(draw, dict) else None
        if dhw_kw is None:
            await self._put(DHW, keys.scope, None, dhw_start, "learning", cop.NO_DRAW, {}, 0)
            await self._put(BRINE, keys.scope, None, None, "learning", cop.NO_DRAW, {}, 0)
        elif liters is None:
            await self._put(DHW, keys.scope, None, dhw_start, "learning", cop.NO_VOLUME, {}, 0)
            await self._put(BRINE, keys.scope, None, None, "learning", cop.NO_VOLUME, {}, 0)
        else:
            share = cop.learn_dhw(charges, liters, dhw_kw)
            verdict = cop.judge_dhw(charges, liters, dhw_kw, dhw_start)
            await self._verdict(DHW, keys.scope, share, dhw_start, verdict, len(charges))
            w_k = cop.learn_brine(charges, liters, dhw_kw)
            verdict = cop.judge_brine(charges, liters, dhw_kw, dhw_start)
            await self._verdict(BRINE, keys.scope, w_k, None, verdict, len(charges))
        brine = self._learnings.used(BRINE, keys.scope)
        if heat_kw is None:
            await self._put(
                HEATING, keys.scope, None, heating_start, "learning", cop.NO_DRAW, {}, 0
            )
        elif not isinstance(brine, int | float):
            await self._put(
                HEATING, keys.scope, None, heating_start, "learning", cop.NO_BRINE, {}, 0
            )
        else:
            share = cop.learn_heating(hours, brine, heat_kw)
            verdict = cop.judge_heating(hours, brine, heat_kw, heating_start)
            await self._verdict(HEATING, keys.scope, share, heating_start, verdict, len(hours))

    async def _verdict(
        self,
        name: str,
        scope: str,
        value: float | None,
        starting: float | None,
        verdict: Any,
        n: int,
    ) -> None:
        if value is None:
            args: dict[str, float | int | str] = {"seen": n, "least": cop.WARM}
            await self._put(name, scope, None, starting, "learning", cop.FEW, args, n)
            return
        state: State = "proved" if verdict.proved else "learning"
        await self._put(name, scope, value, starting, state, verdict.why, dict(verdict.args), n)

    async def _put(
        self,
        name: str,
        scope: str,
        value: float | None,
        starting: float | None,
        state: State,
        why: str | None,
        args: dict[str, float | int | str],
        n: int,
    ) -> None:
        before = self._learnings.get(name, scope)
        await self._learnings.put(
            Learned(
                name=name,
                scope=scope,
                kind="model",
                value=value,
                unit="W/K" if name == BRINE else None,
                state=state,
                why=why,
                args=args,
                n=n,
                since=before.since if before and before.since else None,
                starting=starting,
            )
        )

    async def _starting(self, scope: str, heating: float | None, dhw: float | None) -> None:
        """Where nothing can be learned yet: the documented shares, as starting values."""
        for name, start in ((HEATING, heating), (DHW, dhw)):
            found = self._learnings.get(name, scope)
            if found is not None and found.value is not None:
                continue
            await self._learnings.put(
                Learned(name=name, scope=scope, kind="model", state="starting", starting=start)
            )

    def _loss(self, tank_scope: str | None) -> float:
        found = self._learnings.get(LOSS, tank_scope) if tank_scope else None
        value = found.value if found is not None else None
        return float(value) if isinstance(value, int | float) else 0.0

    async def _draws(self, tank_scope: str, since: float, until: float) -> list[Span]:
        found = await self._events(tank_scope, "draw", since - 3600)
        return [(b["start"], b["end"]) for t, b in found if t < until]

    async def _first_raw(self, key: Key) -> float | None:
        def read(t: Transaction) -> float | None:
            row = t.execute(
                "SELECT MIN(t) FROM history WHERE instance = ? AND point = ?",
                (key.instance, key.point),
            ).fetchone()
            return None if row is None else row[0]

        return await self._db.run(read)

    async def _last_day(self, scope: str) -> date | None:
        found = await self._events(scope, "cop_day", 0.0)
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


def _below(nodes: dict[str, Any], unit: str, kind: str) -> list[str]:
    return sorted(p for p, n in nodes.items() if n.kind == kind and p.startswith(f"{unit}/"))


def _point(instance: str, paths: set[str], node: str | None, name: str) -> Key | None:
    path = f"{node}/{name}"
    return Key(instance, path) if node and path in paths else None


def _run(
    t: float, stretch: list[Span], water: tank.Series, brine_in: tank.Series, brine_out: tank.Series
) -> cop.Run | None:
    """A minute at a time over `stretch`: the water made, the brine in and its drop."""
    made: list[float] = []
    source: list[float] = []
    drop = 0.0
    run_s = 0.0
    for t0, t1 in stretch:
        at = t0 + SAMPLE_S / 2
        while at < t1:
            w, b0, b1 = water.at(at), brine_in.at(at), brine_out.at(at)
            if w is not None and b0 is not None and b1 is not None:
                made.append(w)
                source.append(b0)
                drop += (b0 - b1) * SAMPLE_S
                run_s += SAMPLE_S
            at += SAMPLE_S
    if not made:
        return None
    return cop.Run(t, run_s, sum(made) / len(made), sum(source) / len(source), drop)


def _body(run: cop.Run) -> dict[str, Any]:
    return {
        "run_s": run.run_s,
        "supply_c": round(run.supply_c, 2),
        "source_c": round(run.source_c, 2),
        "brine_ks": round(run.brine_ks, 1),
    }


def _unbody(t: float, body: dict[str, Any]) -> cop.Run:
    return cop.Run(
        t,
        body["run_s"],
        body["supply_c"],
        body["source_c"],
        body["brine_ks"],
        body.get("rise_c", 0.0),
    )


def _events(t: Transaction, scope: str, rows: list[tuple[str, float, dict[str, Any]]]) -> None:
    t.executemany(
        "INSERT OR REPLACE INTO learn_events (scope, kind, t, body) VALUES (?, ?, ?, ?)",
        [(scope, kind, at, json.dumps(body)) for kind, at, body in rows],
    )
    t.execute("DELETE FROM learn_events WHERE t < ?", (clock.time() - KEEP_S,))
