"""What the learners would have made of a house, for the optimizer to plan with: fitted on
ten days of the same house recorded beforehand, and kept as proved.

The house, its water and its emitters are fitted and judged as the learners do; the draws and
the COP are measured in the recording; the tank's standing loss and its week are what the
tank learner finds within its tolerance, taken from the scenario.

The forecast is the plant's own weather, off as a provider's is: by an error that grows
with how far ahead it looks. Told the truth (hindsight), it isn't, and the week of draws
is each day's own."""

import random
import statistics
from collections.abc import Sequence
from datetime import date, datetime, timedelta

from thermaestro import clock
from thermaestro.core.weather import Forecast, Span
from thermaestro.learn import Learned, Learnings, emitters, house, pump
from thermaestro.learn.efficiency import DHW, HEATING
from thermaestro.learn.emitters import EMITTERS, STORED
from thermaestro.learn.energy import DRAW
from thermaestro.learn.heating import MODEL, RESPONSE, TAU, WATER
from thermaestro.learn.hotwater import DEMAND, LOSS
from thermaestro.learn.tank import Demand

from .hourly import Recorded, shifted
from .model import WATER_J_KG_K, WeatherSeries, begin
from .scenario import Draw, Scenario, draws_on

UNIT = "pump:hp1"
SYSTEM = "pump:hp1/cs1"
TANK = "pump:hp1/dhw"
LEARNED_DAYS = 10


_FITTED: dict[str, dict[tuple[str, str], object]] = {}


def fitted(scenario: Scenario) -> dict[tuple[str, str], object]:
    """Each learned value, by name and scope."""
    key = scenario.to_json()
    if key not in _FITTED:
        _FITTED[key] = _fit(scenario)
    return _FITTED[key]


def _fit(scenario: Scenario) -> dict[tuple[str, str], object]:
    """What could be learned: a house whose heating never ran (summer, the heating stop)
    teaches nothing of itself, as on a real pump."""
    seen = shifted(begin(scenario), LEARNED_DAYS, scenario.seed)
    tank = scenario.tank
    # The standing loss: the share of the tank's lead over its room lost an hour.
    loss = tank.loss_w_k * 3600 / (WATER_J_KG_K * tank.liters)
    week = Demand()
    by_hour = _hourly(scenario, scenario.draws)
    for weekday in range(7):
        for _ in range(4):
            week.learn(weekday, by_hour)
    out: dict[tuple[str, str], object] = {
        (LOSS, TANK): round(loss, 5),
        (DEMAND, TANK): week.to_value(),
        (DHW, UNIT): round(statistics.median(seen.dhw_share), 4),
        (DRAW, UNIT): {"dhw": round(statistics.median(seen.dhw_kw), 3)},
    }
    # Each judged as a pump judges it: fitted on the hours before its last few, tested on
    # them. Only what proves itself is taught.
    end = seen.hours[-1].t + 3600
    fit, house_verdict, _, _ = house.judge(seen.hours, end - house.TEST_HOURS * 3600)
    water, water_verdict, _, _ = pump.judge_water(seen.hours, end - pump.TEST_HOURS * 3600)
    pairs = emitter_pairs(seen)
    found, emitters_verdict = emitters.judge(pairs, end - emitters.TEST_HOURS * 3600)
    verdicts = (house_verdict, water_verdict, emitters_verdict)
    proved = all(v is not None and v.proved for v in verdicts)
    if proved and fit is not None and water is not None and found is not None and seen.drawn_kw:
        k, c = found
        out |= {
            (MODEL, SYSTEM): fit.as_value(),
            (TAU, SYSTEM): fit.tau_h,
            (RESPONSE, SYSTEM): float(fit.lag),
            (WATER, SYSTEM): water.as_value(),
            (EMITTERS, SYSTEM): round(k, 4),
            (STORED, SYSTEM): round(c, 3),
            (HEATING, UNIT): round(statistics.median(seen.heating_share), 4),
            (DRAW, UNIT): {
                "heating": round(statistics.median(seen.drawn_kw), 3),
                "dhw": round(statistics.median(seen.dhw_kw), 3),
            },
        }
    return out


def emitter_pairs(seen: Recorded) -> list[emitters.Pair]:
    """Each hour's heat, as the emitters' learner pairs it: the hours the compressor heated
    a good part of, and those it didn't heat at all."""
    out = []
    for before, x, q in zip(seen.hours, seen.hours[1:], seen.heat_kw[1:], strict=False):
        if x.water is None or x.room is None or before.water is None:
            continue
        if x.heating is not None and (x.heating > 0.15 or x.heating < emitters.IDLE):
            out.append(emitters.Pair(x.t, x.water - x.room, q, x.water - before.water))
    return out


def _hourly(scenario: Scenario, draws: Sequence[Draw]) -> list[float]:
    """What the draws take from the tank in each hour: a draw takes its share of the tank
    down from about 48 °C to the cold water's."""
    tank = scenario.tank
    by_hour = [0.0] * 24
    for d in draws:
        by_hour[int(d.hour) % 24] += d.liters / tank.liters * (48.0 - tank.cold_c)
    return by_hour


async def teach(
    learnings: Learnings, scenario: Scenario, days: Sequence[date] | None = None
) -> None:
    """What the learners would have made of the house. Given `days` (a week at most), the
    week of draws is those days' own: what was drawn, known beforehand."""
    found = dict(fitted(scenario))
    if days is not None:
        week = Demand.from_value(found[(DEMAND, TANK)])
        for day in days:
            w = day.weekday()
            week.hours[w * 24 : w * 24 + 24] = _hourly(scenario, draws_on(scenario, day))
        found[(DEMAND, TANK)] = week.to_value()
    for (name, scope), value in found.items():
        await learnings.put(
            Learned(name=name, scope=scope, kind="model", value=value, state="proved")
        )


ISSUE_S = 6 * 3600.0
"""A forecast is issued every six hours."""
SPREAD = {"temperature": (0.3, 2.0), "irradiance.global": (0.1, 0.5), "wind_speed": (0.5, 2.0)}
"""How far off a forecast is, typically: at once and two days ahead; °C, the share of the
sun, m/s."""
PERSISTENCE = 0.9
"""How much of an hour's error carries into the next: a forecast is off for hours at a time."""


class PlantWeather:
    """The plant's own weather as the forecast, hour by hour as a provider gives it: off
    by an error that grows with how far ahead the hour is from the forecast's issue, unless
    `truth`."""

    def __init__(self, scenario: Scenario, *, truth: bool = False) -> None:
        self._series = WeatherSeries(scenario)
        self._seed = scenario.seed
        self._truth = truth
        self._errors: dict[tuple[str, float], list[float]] = {}

    def _off(self, quantity: str, issued: float) -> list[float]:
        """The forecast's errors from its issue on, an hour at a time, each of spread one."""
        if (quantity, issued) not in self._errors:
            rng = random.Random(f"{self._seed}/{quantity}/{issued}")
            keep = PERSISTENCE
            e = rng.gauss(0, 1)
            out = []
            for _ in range(72):
                out.append(e)
                e = keep * e + (1 - keep**2) ** 0.5 * rng.gauss(0, 1)
            self._errors[(quantity, issued)] = out
        return self._errors[(quantity, issued)]

    async def forecast(self, quantity: str, start: datetime, end: datetime) -> Forecast:
        truth = {
            "temperature": self._series.outdoor,
            "irradiance.global": self._series.sun,
            "wind_speed": self._series.wind,
        }.get(quantity)
        if truth is None:
            return Forecast(quantity, "plant", spans=[])
        issued = clock.time() // ISSUE_S * ISSUE_S
        spans = []
        t = start.replace(minute=0, second=0, microsecond=0)
        while t < end:
            at = t.timestamp()
            value = truth(at)
            if not self._truth:
                ahead = max(0.0, (at - issued) / 3600)
                near, far = SPREAD[quantity]
                off = (near + (far - near) * min(ahead, 48.0) / 48) * self._off(quantity, issued)[
                    min(int(ahead), 71)
                ]
                if quantity == "temperature":
                    value += off
                elif quantity == "irradiance.global":
                    value *= max(0.0, 1 + off)
                else:
                    value = max(0.0, value + off)
            spans.append(Span(t, t + timedelta(hours=1), value))
            t += timedelta(hours=1)
        return Forecast(quantity, "plant", spans=spans)
