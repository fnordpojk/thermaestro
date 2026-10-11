"""What a kWh of electricity gives in heat: the compressor's COP, from its documented
figures and from what it is seen to do.

A COP is described as a share of the ideal (Carnot's) between the source, the brine in or
the outdoor air, and the water made: `cop = share · (supply + 273.15) / (supply - source)`.
Nibe's own figures for the F1245 give a share of about half at both 35 and 45 °C, so one
share per purpose, heating and making hot water, describes them; what is learned on a
pump is that share.

- **Hot water**, from charges with no draw: the tank's rise in degrees (the mean of its two
  sensors) times its heat per degree, plus its standing loss over the charge, against the
  compressor's learned draw times how long it ran on hot water. The water made is the
  charge sensor's, which shows the charge while it runs.
- **The brine**, from the same charges: the heat that didn't come from electricity came
  from the ground, so the brine's heat per kelvin is that heat over the brine's drop,
  summed over the time the compressor ran. A ground-source pump's brine flow is seldom
  known; this measures it.
- **Heating**, from hours the compressor heated the house: the brine's heat, once proved,
  plus the draw, against the draw.

Each is judged on data it wasn't fitted on, each charge or hour predicted from the ones
before it only: hot water's share by the charge's COP, the brine by the charge's heat,
heating's share by the hour's brine drop; each against the documented figure (or, where
there is none, the median seen).
"""

import statistics
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from .model import mark
from .validate import Verdict, held_out

KELVIN = 273.15
LIFT_LEAST = 5.0
"""Below this lift the ideal COP means nothing; it is taken as at least this."""
DHW_SUPPLY_C = 55.0
"""The water a documented hot-water COP (EN 16147) is reckoned to be made at, about."""
WATER_KJ_L_K = 4.186
RUN_LEAST_S = 600.0
"""A charge or an hour needs the compressor this long on its purpose to count."""
WARM = 5
"""Runs seen before a share is given, and before the first is predicted."""
LEAST = 8
"""Comparisons a verdict needs: a charge with no draw near it may come once a day."""

FEW = mark("%(seen)s of %(least)s charges or hours seen to learn from.")
NO_DRAW = mark("Learned once the compressor's draw has proved itself.")
NO_VOLUME = mark("Learned once the tank's volume is known: enter it under House.")
NO_BRINE = mark("Learned once the brine's heat per kelvin has proved itself.")


def carnot(supply_c: float, source_c: float) -> float:
    """The ideal COP making water at `supply_c` from a source at `source_c`."""
    return (supply_c + KELVIN) / max(LIFT_LEAST, supply_c - source_c)


def share(points: Sequence[tuple[float, float, float]]) -> float | None:
    """The share of the ideal that documented `(source, supply, cop)` points give."""
    found = [cop / carnot(supply, source) for source, supply, cop in points]
    return round(sum(found) / len(found), 4) if found else None


def dhw_share(cop_dhw: float, source_c: float = 0.0) -> float:
    """The share a documented hot-water COP gives, at brine 0 °C."""
    return round(cop_dhw / carnot(DHW_SUPPLY_C, source_c), 4)


@dataclass(frozen=True, slots=True)
class Run:
    """A stretch the compressor served one purpose: a charge, or a heating hour."""

    t: float
    run_s: float
    """How long the compressor ran on the purpose."""
    supply_c: float
    """The water made, on average while it ran."""
    source_c: float
    """The brine in, on average while it ran."""
    brine_ks: float
    """The brine's drop, summed over the time it ran: K·s."""
    rise_c: float = 0.0
    """A charge's rise in degrees of the tank, its standing loss added; 0 for an hour."""

    def kwh(self, draw_kw: float) -> float:
        return draw_kw * self.run_s / 3600

    @property
    def ideal(self) -> float:
        return carnot(self.supply_c, self.source_c)


def tank_kwh(run: Run, liters: float) -> float:
    return run.rise_c * liters * WATER_KJ_L_K / 3600


def charge_cop(run: Run, liters: float, draw_kw: float) -> float | None:
    used = run.kwh(draw_kw)
    return tank_kwh(run, liters) / used if used > 0 else None


def brine_w_k(run: Run, liters: float, draw_kw: float) -> float | None:
    """What the brine carries per kelvin, from a charge: the ground's share of its heat
    over the brine's drop."""
    ground = tank_kwh(run, liters) - run.kwh(draw_kw)
    if run.brine_ks <= 0 or ground <= 0:
        return None
    return ground * 3.6e6 / run.brine_ks


def _median(values: Sequence[float | None]) -> float | None:
    found = [v for v in values if v is not None]
    return statistics.median(found) if found else None


def learn_dhw(charges: Sequence[Run], liters: float, draw_kw: float) -> float | None:
    """Hot water's share of the ideal: the median over the charges."""
    found = [(ch, charge_cop(ch, liters, draw_kw)) for ch in charges if ch.run_s >= RUN_LEAST_S]
    shares = [cop / ch.ideal for ch, cop in found if cop is not None]
    return round(statistics.median(shares), 4) if len(shares) >= WARM else None


def learn_brine(charges: Sequence[Run], liters: float, draw_kw: float) -> float | None:
    found = [brine_w_k(ch, liters, draw_kw) for ch in charges if ch.run_s >= RUN_LEAST_S]
    kept = [w for w in found if w is not None]
    return round(statistics.median(kept), 1) if len(kept) >= WARM else None


def hour_share(run: Run, w_k: float, draw_kw: float) -> float | None:
    used = run.kwh(draw_kw)
    if used <= 0:
        return None
    heat = used + w_k * run.brine_ks / 3.6e6
    return heat / used / run.ideal


def learn_heating(hours: Sequence[Run], w_k: float, draw_kw: float) -> float | None:
    found = [hour_share(h, w_k, draw_kw) for h in hours if h.run_s >= RUN_LEAST_S]
    kept = [s for s in found if s is not None]
    return round(statistics.median(kept), 4) if len(kept) >= WARM else None


def _brine_ks(run: Run, share_: float, w_k: float, draw_kw: float) -> float:
    """The brine's drop over a run, if the compressor makes heat at `share_` of the ideal."""
    return run.kwh(draw_kw) * (share_ * run.ideal - 1) * 3.6e6 / w_k


def _rolling(
    runs: Sequence[Run],
    learn: Callable[[Sequence[Run]], float | None],
    predict: Callable[[Run, float], float],
    baseline: Callable[[Run, Sequence[Run]], float | None],
    actual: Callable[[Run], float | None],
) -> Verdict:
    """Each run predicted from the runs before it only, against the baseline."""
    usable = [r for r in runs if r.run_s >= RUN_LEAST_S]
    predicted: list[float | None] = []
    base: list[float | None] = []
    seen: list[float | None] = []
    for i, run in enumerate(usable):
        if i < WARM:
            continue
        learned = learn(usable[:i])
        predicted.append(predict(run, learned) if learned is not None else None)
        base.append(baseline(run, usable[:i]))
        seen.append(actual(run))
    return held_out(predicted, base, seen, least=LEAST)


def judge_dhw(
    charges: Sequence[Run], liters: float, draw_kw: float, documented: float | None
) -> Verdict:
    """Hot water's share predicting each charge's COP, against the documented share (or
    the median COP seen)."""

    def baseline(run: Run, before: Sequence[Run]) -> float | None:
        if documented is not None:
            return documented * run.ideal
        return _median([charge_cop(b, liters, draw_kw) for b in before])

    return _rolling(
        charges,
        lambda before: learn_dhw(before, liters, draw_kw),
        lambda run, share_: share_ * run.ideal,
        baseline,
        lambda run: charge_cop(run, liters, draw_kw),
    )


def judge_brine(
    charges: Sequence[Run], liters: float, draw_kw: float, documented: float | None
) -> Verdict:
    """The brine predicting each charge's heat from its electricity and the brine's drop,
    against the documented hot-water share (or the median charge's heat)."""

    def baseline(run: Run, before: Sequence[Run]) -> float | None:
        if documented is not None:
            return run.kwh(draw_kw) * documented * run.ideal
        return _median([tank_kwh(b, liters) for b in before])

    return _rolling(
        charges,
        lambda before: learn_brine(before, liters, draw_kw),
        lambda run, w_k: run.kwh(draw_kw) + w_k * run.brine_ks / 3.6e6,
        baseline,
        lambda run: tank_kwh(run, liters),
    )


def judge_heating(
    hours: Sequence[Run], w_k: float, draw_kw: float, documented: float | None
) -> Verdict:
    """Heating's share predicting each hour's brine drop, against the documented share (or
    the median drop a second seen)."""

    def baseline(run: Run, before: Sequence[Run]) -> float | None:
        if documented is not None:
            return _brine_ks(run, documented, w_k, draw_kw)
        rate = _median([b.brine_ks / b.run_s for b in before])
        return rate * run.run_s if rate is not None else None

    return _rolling(
        hours,
        lambda before: learn_heating(before, w_k, draw_kw),
        lambda run, share_: _brine_ks(run, share_, w_k, draw_kw),
        baseline,
        lambda run: run.brine_ks,
    )
