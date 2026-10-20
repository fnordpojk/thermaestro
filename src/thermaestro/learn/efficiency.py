"""The compressor's COP, learned day by day from what is recorded.

Once a day is over (and an hour more), its hot-water charges with no draw nearby and its
hours of heating are read: how long the compressor ran on each, the water it made, the
brine in and the brine's drop, and for a charge the tank's rise. They are kept as events,
so what is learned from them can be worked out again as the draw and the brine are.

The starting shares come from what the pump's plugin documents, or what the household
entered under House. A pump needs the demand, the compressor's state and its source (the
brine's two temperatures, or the outdoor air for a pump without brine) to have passed their
checks, its tank's two temperatures to read charges or a climate system's supply to read
heating, and its draw to have proved itself. A pump without brine has nothing that measures
its heat: its heating share stays as documented, and its hours are kept for what the
emitters give.
"""

import json
import logging
from collections.abc import Callable, Iterator
from dataclasses import dataclass, replace
from datetime import date, timedelta
from typing import Any

from .. import clock
from ..cap.model import Rating
from ..core.host import PluginHost
from ..core.values import Key, Sample, Values
from ..store import Database, Rated, Transaction
from . import cop, tank
from .electric import counter_kwh
from .energy import DRAW
from .hotwater import KEEP_S, LOSS, house
from .inputs import Span, intersect, seconds, spans
from .meters import pump_meters
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
    meter: Key | None = None
    """An electricity meter on the pump's supply, in kWh, where one is set up."""
    outdoor: Key | None = None
    """The outdoor temperature: the source of a pump without brine."""


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
                    outdoor=_point(instance_id, paths, unit, "outdoor.temp"),
                )

    async def run(self, now: float | None = None) -> None:
        now = clock.time() if now is None else now
        calendar, _ = await house(self._db)
        rated = await self._db.get(Rated) or Rated()
        meters = await pump_meters(self._db)
        for keys in self.pumps():
            keys = replace(keys, meter=meters.get(keys.scope))
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
        heating_start, dhw_start = self.starting(keys, rated)
        if not self._readable(keys):
            await self._starting(keys.scope, heating_start, dhw_start)
            return
        first = await self._first_raw(keys.compressor)
        if first is None:
            return
        day = tank.first_day(first, await self._last_day(keys.scope), zone)
        while tank.bounds(day, zone)[1] <= now - 3600:
            await self._read(keys, day, zone)
            day += timedelta(days=1)
        await self._judge(keys, rated, now, heating_start, dhw_start)

    def _readable(self, keys: PumpKeys) -> bool:
        """Whether its runs can be read: the compressor, the demand and its source (the
        brine in and out, or the outdoor air for a pump without brine), each usable; and a
        tank to charge or a climate system to heat."""
        brined = keys.brine_in is not None or keys.brine_out is not None
        source = [keys.brine_in, keys.brine_out] if brined else [keys.outdoor]
        needed = [keys.compressor, keys.demand, *source]
        if any(k is None for k in needed) or not all(self._usable(k) for k in needed if k):
            return False
        return self._tank(keys) or (keys.supply is not None and self._usable(keys.supply))

    def _tank(self, keys: PumpKeys) -> bool:
        """Whether its tank's charges can be read."""
        return (
            keys.top is not None
            and keys.low is not None
            and self._usable(keys.top)
            and self._usable(keys.low)
        )

    async def _read(self, keys: PumpKeys, day: date, zone: Any) -> None:
        start, end = tank.bounds(day, zone)
        since, until = start - 3600, end + 3600

        async def history(key: Key) -> list[Sample]:
            return await self._values.history(key, since, until)

        charged = self._tank(keys)
        top = tank.Series(await history(keys.top) if charged and keys.top else [])
        low = tank.Series(await history(keys.low) if charged and keys.low else [])
        # A pump without brine takes its heat from the outdoor air.
        inlet = keys.brine_in if keys.brine_in is not None else keys.outdoor
        source = tank.Series(await history(inlet) if inlet is not None else [])
        returning = tank.Series(await history(keys.brine_out)) if keys.brine_out else None
        supply = tank.Series(await history(keys.supply)) if keys.supply else None
        counter = (
            tank.Series(await history(keys.meter))
            if keys.meter is not None and self._usable(keys.meter)
            else None
        )
        demand = await history(keys.demand)
        ran = spans(await history(keys.compressor), until, lambda s: s.text == "running")
        on_dhw = intersect(ran, spans(demand, until, lambda s: s.text == "dhw"))
        on_heat = intersect(ran, spans(demand, until, lambda s: s.text == "heating"))
        draws = await self._draws(keys.tank, since, until) if keys.tank else []
        rows: list[tuple[str, float, dict[str, Any]]] = [
            ("cop_day", start, {"day": day.isoformat()})
        ]
        for c in tank.charges(demand, until, top):
            if not start <= c.start < end:
                continue
            settled = c.end + tank.SETTLE_S
            if any(d0 < settled and c.start - tank.SETTLE_S < d1 for d0, d1 in draws):
                continue  # a draw would hide part of what the charge made
            before, after = tank._mean(top, low, c.start - 60), tank._mean(top, low, settled)
            stretch = intersect(on_dhw, [(c.start, c.end)])
            run = _run(c.start, stretch, low, source, returning, counter)
            if before is None or after is None or run is None:
                continue
            # The loss is added when judged, as known then: one learned later still counts.
            loss_kh = ((before + after) / 2 - tank.ROOM_C) * (settled - c.start) / 3600
            extra = {"rise_c": round(after - before, 3), "loss_kh": round(loss_kh, 3)}
            rows.append(("cop_charge", c.start, _body(run) | extra))
        if supply is not None:
            t = start
            while t < end:
                stretch = intersect(on_heat, [(t, t + 3600)])
                run = _run(t, stretch, supply, source, returning, counter)
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
        k = self._loss(keys.tank)
        charges = [
            _unbody(t, b, k)
            for t, b in await self._events(keys.scope, "cop_charge", now - DAYS * 86_400)
        ]
        hours = [
            _unbody(t, b)
            for t, b in await self._events(keys.scope, "cop_hour", now - DAYS * 86_400)
        ]
        dhw_kw = draw.get("dhw") if isinstance(draw, dict) else None
        heat_kw = draw.get("heating") if isinstance(draw, dict) else None
        # Metered, the electricity is in each run: no draw needed.
        if charges and all(c.measured_kwh is not None for c in charges):
            dhw_kw = dhw_kw or 0.0
        if hours and all(h.measured_kwh is not None for h in hours):
            heat_kw = heat_kw or 0.0
        brined = keys.brine_in is not None and keys.brine_out is not None
        if keys.top is not None and keys.low is not None:  # a tank whose charges are read
            if dhw_kw is None:
                await self._put(DHW, keys.scope, None, dhw_start, "learning", cop.NO_DRAW, {}, 0)
                if brined:
                    await self._put(BRINE, keys.scope, None, None, "learning", cop.NO_DRAW, {}, 0)
            elif liters is None:
                await self._put(DHW, keys.scope, None, dhw_start, "learning", cop.NO_VOLUME, {}, 0)
                if brined:
                    await self._put(BRINE, keys.scope, None, None, "learning", cop.NO_VOLUME, {}, 0)
            else:
                share = cop.learn_dhw(charges, liters, dhw_kw)
                verdict = cop.judge_dhw(charges, liters, dhw_kw, dhw_start)
                await self._verdict(DHW, keys.scope, share, dhw_start, verdict, len(charges))
                if brined:
                    w_k = cop.learn_brine(charges, liters, dhw_kw)
                    verdict = cop.judge_brine(charges, liters, dhw_kw, dhw_start)
                    await self._verdict(BRINE, keys.scope, w_k, None, verdict, len(charges))
        brine = self._learnings.used(BRINE, keys.scope)
        if not brined:
            await self._put(
                HEATING, keys.scope, None, heating_start, "learning", cop.NO_SOURCE, {}, 0
            )
        elif heat_kw is None:
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
    t: float,
    stretch: list[Span],
    water: tank.Series,
    inlet: tank.Series,
    returning: tank.Series | None,
    counter: tank.Series | None = None,
) -> cop.Run | None:
    """The time the compressor ran over `stretch`; the water made and the source (the brine
    in, or the outdoor air) on average a minute at a time where each was good; and the
    brine's drop over the whole run, where there is brine coming back. A reading not good for
    a while (the charge sensor while tank-bottom water passes it, the supply as the
    compressor starts) shortens neither the run nor the drop."""
    made: list[float] = []
    source: list[float] = []
    drops: list[float] = []
    for t0, t1 in stretch:
        at = t0 + SAMPLE_S / 2
        while at < t1:
            w, b0 = water.at(at), inlet.at(at)
            b1 = returning.at(at) if returning is not None else None
            if w is not None:
                made.append(w)
            if b0 is not None:
                source.append(b0)
                if b1 is not None:
                    drops.append(b0 - b1)
            at += SAMPLE_S
    if not made or not source:
        return None
    run_s = seconds(stretch)
    drop = sum(drops) / len(drops) * run_s if drops else 0.0
    measured = None
    if counter is not None:
        parts = [counter_kwh(counter, t0, t1) for t0, t1 in stretch]
        measured = (
            sum(p for p in parts if p is not None) if all(p is not None for p in parts) else None
        )
    return cop.Run(
        t, run_s, sum(made) / len(made), sum(source) / len(source), drop, measured_kwh=measured
    )


def _body(run: cop.Run) -> dict[str, Any]:
    return {
        "run_s": run.run_s,
        "supply_c": round(run.supply_c, 2),
        "source_c": round(run.source_c, 2),
        "brine_ks": round(run.brine_ks, 1),
        "kwh": None if run.measured_kwh is None else round(run.measured_kwh, 4),
    }


def _unbody(t: float, body: dict[str, Any], loss: float = 0.0) -> cop.Run:
    """A kept run; a charge's rise with the tank's standing loss (per hour, per degree
    over the room) added as `loss` says. One kept before the loss was kept apart has it
    in its rise already."""
    return cop.Run(
        t,
        body["run_s"],
        body["supply_c"],
        body["source_c"],
        body["brine_ks"],
        body.get("rise_c", 0.0) + loss * body.get("loss_kh", 0.0),
        body.get("kwh"),
    )


def _events(t: Transaction, scope: str, rows: list[tuple[str, float, dict[str, Any]]]) -> None:
    t.executemany(
        "INSERT OR REPLACE INTO learn_events (scope, kind, t, body) VALUES (?, ?, ?, ?)",
        [(scope, kind, at, json.dumps(body)) for kind, at, body in rows],
    )
    t.execute("DELETE FROM learn_events WHERE t < ?", (clock.time() - KEEP_S,))
