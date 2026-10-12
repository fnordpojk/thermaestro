"""The plan over the horizon: a linear program, solved rank by rank.

**What it decides:** per climate system and hour, the heating offset (as a number, not yet
on the lever's steps) and so the water; per slot, the share of the slot the compressor
heats each climate system, charges each tank and heats each pool (together at most the
whole slot: it serves one at a time); and the addition's kW per hour.

**How the house follows** is the learned two-part model (`learn.house`), hour by hour: the
rooms pulled toward the water (its mean over the system's lag) and the outdoor
temperature, the building's mass following the rooms. The water follows the offset by the
learned curve (`learn.pump.Water`), keeping a share of where it was an hour before, and
gives the house `K · (water - room)` kW, `K` the emitters' learned output; the compressor
and the addition must give that heat. The offset rises at most a step an hour: faster,
the pump's degree minutes run deep and its addition starts.

**What it costs:** the compressor's draw while it runs, its heat the draw times its COP at
the slot's source and the water it makes (a share of the ideal, learned or documented);
the addition at COP 1. A tank's level follows its standing loss and the draws expected; a
pool its learned warming and cooling.

**What gives way** is solved in stages: the protection floors first, then the ranking in
the household's order (comfort's low edges, `must` and `should` hot-water deadlines,
comfort's high edges, the power limit), then the pools' bands, and last cost against
closeness to each band's middle, by the slider. Every household goal is soft, its slack in
its own unit (°C·h below a band, °C short at a deadline, kWh over a limit), so the plan
always exists and says what falls short.

The COP depends on the water planned, so the plan is solved twice: first at the water of
the offset as it stands, then at the water the first solve planned.
"""

import statistics
import time
from collections.abc import Sequence
from dataclasses import dataclass, field, replace

from ..learn.cop import carnot
from ..learn.house import Hour
from .problem import (
    SLOT_H,
    Plan,
    PoolPlan,
    Problem,
    SystemPlan,
    TankPlan,
)
from .solver import INF, Program, Solved, Stage

FLOORS = "floors"
POOLS = "pools"
SLIDER = "slider"
TOLERANCE = 0.001
"""How far above its optimum a stage may be let go by the ones after, in its own unit
(°C·h, °C, kWh): next to nothing, so what an earlier stage reached isn't traded away, but
not none, which a solver's rounding can't keep."""
CALM = 0.002
"""What each degree of change in a planned offset costs in the last stage: enough that it
doesn't wander between equal plans."""
UNPRICED = 0.01
"""Without prices or a cost stance, energy still counts, a little: no heat for nothing."""
REFINE = 1
RAMP = 1.0
"""The most a planned offset rises in an hour, in steps."""
AHEAD_OF_WATER = 1.0
"""How far above its water a climate system may be asked to settle, °C."""


@dataclass
class _Built:
    program: Program
    stages: list[Stage]
    offset: dict[str, list[int]] = field(default_factory=dict)
    room: dict[str, list[int]] = field(default_factory=dict)
    water: dict[str, list[int]] = field(default_factory=dict)
    x: dict[str, list[int]] = field(default_factory=dict)
    add: dict[str, list[int]] = field(default_factory=dict)
    y: dict[str, list[int]] = field(default_factory=dict)
    level: dict[str, list[int]] = field(default_factory=dict)
    z: dict[str, list[int]] = field(default_factory=dict)
    temp: dict[str, list[int]] = field(default_factory=dict)
    kw: list[dict[int, float]] = field(default_factory=list)
    """The pump's electricity per slot, by column."""
    heat: dict[str, list[dict[int, float]]] = field(default_factory=dict)
    """Per system and hour, the heat the pump gives, by column; with `heat_const`."""
    heat_const: dict[str, list[float]] = field(default_factory=dict)
    why: str = ""


def _hourly(values: Sequence[float]) -> list[float]:
    return [sum(values[4 * h : 4 * h + 4]) / 4 for h in range(len(values) // 4)]


def _cop(share: float, made: float, source: float) -> float:
    return max(1.0, share * carnot(made, source))


def _build(p: Problem, supply: dict[str, list[float]]) -> _Built:
    g = Program()
    stages: dict[str, dict[int, float]] = {}
    rank_of = {name: 2 + i for i, name in enumerate(p.ranking)}
    names = {v: k for k, v in rank_of.items()}

    def add(stage: str, column: int, weight: float) -> None:
        stages.setdefault(stage, {})
        stages[stage][column] = stages[stage].get(column, 0.0) + weight

    built = _Built(g, [])
    hours, slots = p.hours, len(p.slots)
    outdoor = _hourly([s.outdoor for s in p.slots])
    sun = _hourly([s.sun for s in p.slots])
    wind = _hourly([s.wind for s in p.slots])
    source = _hourly([s.source for s in p.slots])
    kw: list[dict[int, float]] = [{} for _ in range(slots)]
    load: list[dict[int, float]] = [{} for _ in range(slots)]
    """The compressor's share of each slot, by column."""
    distance: list[tuple[int, float]] = []
    """Columns of distance from the middle of a band, °C·h, with their weight."""
    forced: list[tuple[int, float, int]] = []
    """Columns of °C asked ahead of the water, the kW of the addition's heat a degree of
    it brings, and the hour."""
    calm: list[int] = []

    for s in p.systems:
        f = s.fit
        lag = max(1, int(f.lag))
        f = replace(f, gamma=f.gamma + s.drift)
        here = Hour(p.slots[0].t, s.room, outdoor[0], None, sun[0], wind[0])
        now_water = s.water.water(outdoor[0], s.offset_now)
        before = list(s.water_before) or [now_water]
        lagged_now = sum(before[-lag:]) / len(before[-lag:])
        mass0 = f.mass(s.room, here, lagged_now)
        if mass0 is None:
            built.why = f"{s.scope}: the house model can't place the building's mass"
            continue
        offset = [g.var(s.offset_low, s.offset_high, f"{s.scope} offset {h}") for h in range(hours)]
        room = [g.var(-INF, INF, f"{s.scope} room {h}") for h in range(hours)]
        mass = [g.var(-INF, INF, f"{s.scope} mass {h}") for h in range(hours)]
        water = [g.var(-INF, INF, f"{s.scope} water {h}") for h in range(hours)]
        built.offset[s.scope] = offset
        built.room[s.scope] = room
        built.water[s.scope] = water
        # The water follows the offset, keeping a share of where it was.
        curve = s.water
        for h in range(hours):
            row = {water[h]: 1.0, offset[h]: -curve.w2}
            const = curve.w0 + curve.w1 * outdoor[h]
            if h == 0:
                const += curve.a * before[-1]
            else:
                row[water[h - 1]] = -curve.a
            g.equal(row, const)
            # What the offset asks for (where the water would settle) never more than a
            # little above where the water can be: the pump's degree minutes add up the
            # difference, and once deep enough its addition starts, at a COP of 1. A slab
            # follows slowly, so its offset rises only as fast as its water.
            # Beyond it, the addition's heat at a COP of 1 is what it costs.
            settle = 1.0 - curve.a
            ahead = g.var(0.0, INF, f"{s.scope} ahead of its water {h}")
            g.row(
                {offset[h]: curve.w2 / settle, water[h]: -1.0, ahead: -1.0},
                -INF,
                AHEAD_OF_WATER - (curve.w0 + curve.w1 * outdoor[h]) / settle,
            )
            forced.append((ahead, s.emitters_kw_k, h))
            # And raised at most a step an hour, for the same reason.
            if h == 0:
                g.row({offset[0]: 1.0}, -INF, s.offset_now + RAMP)
            else:
                g.row({offset[h]: 1.0, offset[h - 1]: -1.0}, -INF, RAMP)
        for h in range(hours):
            if h == 0:
                g.equal({mass[0]: 1.0}, mass0 + f.r * (s.room - mass0))
            else:
                g.equal({mass[h]: 1.0, mass[h - 1]: -(1 - f.r), room[h - 1]: -f.r}, 0.0)
            pull = f.beta + f.omega * wind[h] / 10
            keep = 1 - f.alpha - pull
            # The water's mean over the lag: planned hours, and before them what was.
            coefs: dict[int, float] = {room[h]: 1.0, mass[h]: -keep}
            const = pull * outdoor[h] + f.sigma * sun[h] / 100 + f.gamma
            for i in range(h - lag + 1, h + 1):
                if i >= 0:
                    coefs[water[i]] = coefs.get(water[i], 0.0) - f.alpha / lag
                else:
                    known = before[i] if -i <= len(before) else before[0]
                    const += f.alpha * known / lag
            g.equal(coefs, const)
        # The heat the water gives, and who gives it.
        k = s.emitters_kw_k
        xs = [g.var(0.0, 1.0, f"{s.scope} share {j}") for j in range(slots)]
        built.x[s.scope] = xs
        adds: list[int] = []
        if p.addition is not None:
            for h in range(hours):
                most = p.addition.kw
                if p.addition.policy == "limit" and p.addition.limit_kw is not None:
                    most = min(most, p.addition.limit_kw)
                if p.addition.policy == "not_when_expensive" and any(
                    sl.expensive for sl in p.slots[4 * h : 4 * h + 4]
                ):
                    most = 0.0
                adds.append(g.var(0.0, max(0.0, most), f"{s.scope} addition {h}"))
        built.add[s.scope] = adds
        heats: list[dict[int, float]] = []
        consts: list[float] = []
        for h in range(hours):
            # The pump gives at least the heat the water hands the house, K · (water - room),
            # and what warms the emitters themselves, C · the water's rise; nothing where that
            # is below zero, as when a slab cooling heats the rooms with heat stored before.
            # More than the compressor and the addition can give is physics, not a goal: the
            # plan keeps it first.
            given: dict[int, float] = {}
            cop = _cop(s.share, supply[s.scope][h], source[h])
            row = {water[h]: -k - s.emitters_kwh_k, room[h]: k}
            const = 0.0
            if h == 0:
                const = -s.emitters_kwh_k * before[-1]
            else:
                row[water[h - 1]] = s.emitters_kwh_k
            short = g.var(0.0, INF, f"{s.scope} heat short {h}")
            row[short] = 1.0
            add(FLOORS, short, 1.0)
            for j in range(4 * h, 4 * h + 4):
                row[xs[j]] = s.draw_kw * cop * SLOT_H
                given[xs[j]] = s.draw_kw * cop * SLOT_H
            if adds:
                row[adds[h]] = 1.0
                given[adds[h]] = 1.0
            g.row(row, const, INF)
            heats.append(given)
            consts.append(0.0)
        built.heat[s.scope] = heats
        built.heat_const[s.scope] = consts
        for j in range(slots):
            load[j][xs[j]] = 1.0
            kw[j][xs[j]] = kw[j].get(xs[j], 0.0) + s.draw_kw
            if adds:
                kw[j][adds[j // 4]] = kw[j].get(adds[j // 4], 0.0) + 1.0
        # The rooms' bands, and how far from their middles.
        ua = k * f.beta / f.alpha if f.alpha > 0 else 0.0
        reference = _cop(s.share, supply[s.scope][0], source[0])
        per_degree = ua / reference / max(1, len(s.rooms))
        """kWh of electricity a °C·h of one room's distance from its middle is worth."""
        for r in s.rooms:
            for h in range(hours):
                band = r.bands[h] if h < len(r.bands) else r.bands[-1]
                if band.low is not None:
                    short = g.var(0.0, INF, f"{r.scope} below {h}")
                    g.row({room[h]: 1.0, short: 1.0}, band.low - r.off, INF)
                    add("comfort_low", short, 1.0)
                if band.high is not None:
                    over = g.var(0.0, INF, f"{r.scope} above {h}")
                    g.row({room[h]: 1.0, over: -1.0}, -INF, band.high - r.off)
                    add("comfort_high", over, 1.0)
                if band.low is not None and band.high is not None:
                    middle = (band.low + band.high) / 2
                    up = g.var(0.0, INF)
                    down = g.var(0.0, INF)
                    g.equal({room[h]: 1.0, up: -1.0, down: 1.0}, middle - r.off)
                    distance += [(up, per_degree), (down, per_degree)]
        for h in range(hours):
            change = g.var(0.0, INF)
            if h == 0:
                g.row({change: 1.0, offset[0]: -1.0}, -s.offset_now, INF)
                g.row({change: 1.0, offset[0]: 1.0}, s.offset_now, INF)
            else:
                g.row({change: 1.0, offset[h]: -1.0, offset[h - 1]: 1.0}, 0.0, INF)
                g.row({change: 1.0, offset[h]: 1.0, offset[h - 1]: -1.0}, 0.0, INF)
            calm.append(change)

    for t in p.tanks:
        ys = [g.var(0.0, 1.0, f"{t.scope} share {j}") for j in range(slots)]
        top = max(t.ceiling, t.level)
        levels = [g.var(-INF, top, f"{t.scope} level {j}") for j in range(slots)]
        built.y[t.scope], built.level[t.scope] = ys, levels
        for j in range(slots):
            heat = t.draw_kw * _cop(t.share, t.ceiling, p.slots[j].source) * SLOT_H / t.kwh_per_k
            keep = 1 - t.loss * SLOT_H
            row = {levels[j]: 1.0, ys[j]: -heat}
            const = t.loss * t.room_c * SLOT_H - (t.draws[j] if j < len(t.draws) else 0.0)
            if j == 0:
                const += keep * t.level
            else:
                row[levels[j - 1]] = -keep
            g.equal(row, const)
            load[j][ys[j]] = 1.0
            kw[j][ys[j]] = t.draw_kw
            for least in (t.floor, t.least):
                if least is not None:
                    short = g.var(0.0, INF)
                    g.row({levels[j]: 1.0, short: 1.0}, least, INF)
                    add(FLOORS, short, SLOT_H)
        for d in t.deadlines:
            if 0 <= d.slot < slots:
                short = g.var(0.0, INF, f"{t.scope} short {d.slot}")
                g.row({levels[d.slot]: 1.0, short: 1.0}, d.at_least, INF)
                add(names.get(d.rank, "should_deadlines"), short, 1.0)

    for pool in p.pools:
        zs = [g.var(0.0, 1.0, f"{pool.scope} share {j}") for j in range(slots)]
        temps = [g.var(-INF, INF, f"{pool.scope} temp {j}") for j in range(slots)]
        built.z[pool.scope], built.temp[pool.scope] = zs, temps
        for j in range(slots):
            keep = 1 - pool.b * SLOT_H
            row = {temps[j]: 1.0, zs[j]: -pool.a * SLOT_H}
            const = pool.b * p.slots[j].outdoor * SLOT_H
            if j == 0:
                const += keep * pool.temp
            else:
                row[temps[j - 1]] = -keep
            g.equal(row, const)
            load[j][zs[j]] = 1.0
            kw[j][zs[j]] = pool.draw_kw
            band = pool.bands[j] if j < len(pool.bands) else pool.bands[-1]
            if band.low is not None:
                short = g.var(0.0, INF)
                g.row({temps[j]: 1.0, short: 1.0}, band.low, INF)
                add(POOLS, short, SLOT_H)
            if band.high is not None:
                over = g.var(0.0, INF)
                g.row({temps[j]: 1.0, over: -1.0}, -INF, band.high)
                add(POOLS, over, SLOT_H)

    for j, sl in enumerate(p.slots):
        if load[j]:
            g.row(load[j], -INF, 1.0)  # one compressor: one thing at a time
        if sl.power_limit is not None and kw[j]:
            over = g.var(0.0, INF, f"over the limit {j}")
            g.row({**kw[j], over: -1.0}, -INF, sl.power_limit - sl.other_kw)
            add("power_peak", over, SLOT_H)
    built.kw = kw

    # The last stage: cost against closeness, by the slider.
    last: dict[int, float] = {}
    prices = [sl.price for sl in p.slots if sl.price is not None]
    priced = p.slider is not None and len(prices) >= 4
    slider, spread = float(p.slider or 0.0), 1.0
    if priced:
        ordered = sorted(prices)
        spread = ordered[int(0.9 * (len(ordered) - 1))] - ordered[int(0.1 * (len(ordered) - 1))]
        spread = max(spread, 0.05 * abs(statistics.mean(prices)), 1e-3)
    for j, sl in enumerate(p.slots):
        for c, v in kw[j].items():
            if priced and sl.price is not None:
                weight = slider * sl.price * v * SLOT_H / spread
            else:
                weight = UNPRICED * v * SLOT_H
            last[c] = last.get(c, 0.0) + weight
    for c, kw_per_k, h in forced:
        price = statistics.mean(
            [sl.price for sl in p.slots[4 * h : 4 * h + 4] if sl.price is not None] or [0.0]
        )
        per_kwh = slider * price / spread if priced else UNPRICED
        last[c] = last.get(c, 0.0) + max(per_kwh, UNPRICED) * kw_per_k
    closeness = (1 - slider) if priced else 1.0
    for c, w in distance:
        last[c] = last.get(c, 0.0) + closeness * w
    for c in calm:
        last[c] = last.get(c, 0.0) + CALM
    order = [FLOORS, *p.ranking, POOLS]
    built.stages = [Stage(name, stages[name], TOLERANCE) for name in order if name in stages]
    built.stages.append(Stage(SLIDER, last, 0.0))
    return built


def plan(p: Problem, time_limit_s: float = 30.0, refine: int = REFINE) -> Plan:
    """The plan for `p`, or why there is none."""
    began = time.monotonic()
    if p.hours < 1:
        return Plan(False, "no horizon")
    supply = {
        s.scope: [
            (s.water_before[-1] if s.water_before else s.water.water(sl.outdoor, s.offset_now))
            + s.water.lift
            for sl in p.slots[::4]
        ]
        for s in p.systems
    }
    built: _Built | None = None
    solved: Solved | None = None
    for _ in range(refine + 1):
        built = _build(p, supply)
        left = max(1.0, time_limit_s - (time.monotonic() - began))
        solved = built.program.solve(built.stages, left)
        if not solved.ok:
            return Plan(False, solved.why or built.why, seconds=time.monotonic() - began)
        for scope, waters in built.water.items():
            system = next(s for s in p.systems if s.scope == scope)
            supply[scope] = [solved.x[c] + system.water.lift for c in waters]
    if built is None or solved is None:
        return Plan(False, "not solved")
    return _read(p, built, solved, time.monotonic() - began)


def _read(p: Problem, b: _Built, s: Solved, seconds: float) -> Plan:
    x = s.x
    out = Plan(True, b.why, stages=dict(s.values), widened=list(s.widened), seconds=seconds)
    for system in p.systems:
        scope = system.scope
        if scope not in b.offset:
            continue
        sp = SystemPlan(scope)
        sp.offset = [x[c] for c in b.offset[scope]]
        sp.water = [x[c] for c in b.water[scope]]
        sp.room = [x[c] for c in b.room[scope]]
        sp.heat_kw = [
            sum(v * x[c] for c, v in given.items()) + const
            for given, const in zip(b.heat[scope], b.heat_const[scope], strict=True)
        ]
        sp.addition_kw = [x[c] for c in b.add.get(scope, [])]
        sp.share = [x[c] for c in b.x[scope]]
        out.systems[scope] = sp
    for t in p.tanks:
        out.tanks[t.scope] = TankPlan(
            t.scope, [x[c] for c in b.y[t.scope]], [x[c] for c in b.level[t.scope]]
        )
    for pool in p.pools:
        out.pools[pool.scope] = PoolPlan(
            pool.scope, [x[c] for c in b.z[pool.scope]], [x[c] for c in b.temp[pool.scope]]
        )
    out.kw = [sum(v * x[c] for c, v in kw.items()) for kw in b.kw]
    out.cost = sum(
        k * SLOT_H * sl.price for k, sl in zip(out.kw, p.slots, strict=True) if sl.price is not None
    )
    out.stages.pop(SLIDER, None)
    out.stages = {k: max(0.0, round(v, 6)) for k, v in out.stages.items()}
    return out
