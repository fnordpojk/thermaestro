"""A climate system's house, learned hour by hour from what is measured.

Nothing measures the heat a pump gives (its heat meters count only with a flow meter
fitted), so the heat is driven by what is measured: the water in the climate system, the
mean of its supply and return.

**Two parts,** as a house is: the air in its rooms, which follows quickly, and the
building's mass (walls, floors, furniture), which changes slowly. The room is where the
mass is, pulled toward the water and the outdoor temperature:

    room = mass + alpha · (water - mass) - beta · (mass - outdoor)
           + sigma · sun - omega · wind · (mass - outdoor) + gamma

and from one hour to the next the mass moves toward the room by a share `r`. The water is
its mean over the last `lag` hours: a slow system (floor heating in a slab) takes its heat
in over many hours, radiators within the hour. Since the room is measured, the mass behind
it can be worked out each hour; nothing about it is guessed.

What comes out:
- the house's time constant, `1 / (r · beta)` hours: how long, with no heat, it takes to lose
  most of a degree above the outdoor temperature. A reasonably insulated house: tens of
  hours. Outside 3 to 500 hours it isn't a house's, and isn't claimed;
- the system's response, `lag`.

**Fitted** to follow the room six hours ahead, from every other hour, over the hours that
can be used: the room, the outdoor temperature and the water known; no fireplace, no
airing, heating not stopped by the pump's heating stop. The lag is the one that follows
best.

**Can it be told apart?** While the water simply follows the outdoor temperature (as the
pump's curve makes it), what heating gives and what the house loses rise and fall
together, and the fit can't say which is which. Nor is anything learned from rooms that
hardly moved. Then nothing is claimed.

**Proof:** from each hour of the last two days, six hours are simulated with what was
measured going in, and compared with what the room did, against the room simply staying
where it was.

**The household's heat:** people, cooking and computers warm a house too, so a house
emptied cools more than a model learned with someone always home says. A second model has
it as a term, `eta` in each hour someone was home (by its share of the hour), learned from
the hours whose occupancy is known (`learn.occupied`) and only once some had nobody home.
It is judged on the same held-out hours as the model as it is, and used instead only where
it beats it too (`contest`).
"""

import math
import statistics
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace

from .model import mark
from .validate import LEAST, MARGIN, Verdict, held_out, unjudged

LAGS = (1, 2, 3, 4, 6, 8, 12, 18, 24)
"""The responses tried, hours."""
LEAST_HOURS = 72
"""Usable hours a fit needs."""
LEAST_SPREAD = 2.0
"""How far the room-to-outdoor difference must have ranged, °C."""
LEAST_MOVED = 0.4
"""How far the rooms must have moved, °C, for anything to be learned from them."""
TOLD_APART = 0.97
"""Above this correlation between the heating and the loss, they can't be told apart."""
AHEAD = 6
"""Hours simulated, for the fit and the proof."""
TEST_HOURS = 48
SEARCHES = 4
"""The simplex search is begun again from where it stopped, until that finds nothing
better: in six or seven dimensions it settles before it is at the best."""
REFINED = 2
"""The lags searched on so: those that followed best at the first search."""
TAU_H = (3.0, 500.0)
"""A house's time constant lies within these, hours."""

FEW = unjudged(mark("%(hours)s usable hours of %(least)s."))
NARROW = mark("The outdoor temperature has hardly changed: a spread of %(spread)s °C.")
STILL = mark("The rooms have hardly moved: nothing to learn the house from yet.")
TOGETHER = mark(
    "The heating has followed the weather so closely that what it gives and what the house"
    " loses can't be told apart yet."
)
IMPLAUSIBLE = mark("The fit gives a time constant of %(tau)s hours, which isn't a house's.")

AWAY_LEAST = 12
"""Usable hours with time away the household's heat needs to be learned at all: with
someone always home, it can't be told from the rest."""
AWAY_TESTED = 6
"""Held-out comparisons with time away the household's model needs to be judged."""
HOUSEHOLD_MOST_C = 5.0
"""How much warmer a household may keep its rooms, °C: more isn't a household's heat."""
NO_AWAY = unjudged(
    mark("%(hours)s hours with nobody home to learn the household's heat from, of %(least)s.")
)
FEW_AWAY = unjudged(
    mark("%(n)s held-out comparisons with time away so far, of %(least)s, to judge it by.")
)
IMPLAUSIBLE_HOUSEHOLD = mark(
    "The fit gives the household %(settled)s °C of the rooms' warmth, which isn't a household's."
)
OVER = mark(
    "Off by %(error)s on average where the model in use is off by %(current)s: with time away,"
    " %(away)s against %(away_current)s; at home, %(home)s against %(home_current)s."
)
NOT_OVER = mark(
    "Off by %(error)s on average, not clearly better than the model in use, off by"
    " %(current)s: with time away, %(away)s against %(away_current)s; at home, %(home)s"
    " against %(home_current)s."
)
OVER_AWAY = mark(
    "Off by %(error)s on average where the model in use is off by %(current)s, all with time away."
)
NOT_OVER_AWAY = mark(
    "Off by %(error)s on average, not clearly better than the model in use, off by"
    " %(current)s, all with time away."
)


@dataclass(frozen=True, slots=True)
class Hour:
    t: float
    room: float | None
    outdoor: float | None
    water: float | None
    sun: float | None = None
    """W/m², global horizontal."""
    wind: float | None = None
    """m/s."""
    skip: str | None = None
    """Why the hour is left out: "fireplace", "airing", "heating_off"."""
    supply: float | None = None
    """The supply alone, for the pump's curve."""
    offset: float | None = None
    """The heating offset in the hour."""
    heating: float | None = None
    """The share of the hour the compressor heated the house."""
    target: float | None = None
    """The supply temperature the pump's curve calculated, where the pump reports it."""
    back: float | None = None
    """The return as read: whether the water is learned with it is decided when judged."""
    home: float | None = None
    """The share of the hour someone was home (`learn.occupied`); None where not known."""


@dataclass(frozen=True, slots=True)
class Fit:
    alpha: float
    beta: float
    sigma: float
    omega: float
    gamma: float
    r: float
    lag: int
    rows: int
    """Hours the fit followed."""
    error: float
    """Its mean miss six hours ahead, °C."""
    eta: float = 0.0
    """The household's own heat, where learned with it: what it adds to the room in an hour
    someone is home, °C."""
    household: bool = False
    """Whether it was learned with the household's heat as a term."""

    @property
    def tau_h(self) -> float | None:
        rate = self.r * self.beta
        return 1 / rate if rate > 1e-9 else None

    @property
    def settled(self) -> float | None:
        """How much warmer the household keeps the rooms once settled, °C."""
        held = self.alpha + self.beta
        return self.eta / held if self.household and held > 1e-9 else None

    def _pull(self, hour: Hour) -> tuple[float, float]:
        """How far the room is pulled from the mass toward the outdoor temperature, all
        told; and how far the mass counts in the room."""
        out = self.beta + self.omega * (hour.wind or 0.0) / 10
        return out, 1 - self.alpha - out

    def _given(self, hour: Hour, water: float, out: float) -> float:
        """What the water, the outdoor temperature, the sun and the household give the
        room, beside its mass. An hour not known counts as home, as for a model learned
        without the household."""
        return (
            self.alpha * water
            + out * (hour.outdoor or 0.0)
            + self.sigma * (hour.sun or 0.0) / 100
            + self.gamma
            + self.eta * (hour.home if hour.home is not None else 1.0)
        )

    def room(self, mass: float, hour: Hour, water: float) -> float:
        out, keep = self._pull(hour)
        return keep * mass + self._given(hour, water, out)

    def mass(self, room: float, hour: Hour, water: float) -> float | None:
        """The mass behind a room measured."""
        out, keep = self._pull(hour)
        if keep <= 0.05:
            return None
        return (room - self._given(hour, water, out)) / keep

    def as_value(self) -> dict[str, float]:
        found = {
            "alpha": round(self.alpha, 5),
            "beta": round(self.beta, 5),
            "sigma": round(self.sigma, 5),
            "omega": round(self.omega, 5),
            "gamma": round(self.gamma, 5),
            "r": round(self.r, 5),
            "lag": float(self.lag),
            "error": round(self.error, 4),
        }
        if self.household:
            found["eta"] = round(self.eta, 5)
        return found

    @classmethod
    def from_value(cls, value: dict[str, float]) -> "Fit":
        return cls(
            float(value["alpha"]),
            float(value["beta"]),
            float(value["sigma"]),
            float(value["omega"]),
            float(value["gamma"]),
            float(value["r"]),
            int(value["lag"]),
            0,
            float(value.get("error", 0.0)),
            float(value.get("eta", 0.0)),
            "eta" in value,
        )


def lagged(hours: Sequence[Hour], k: int, lag: int) -> float | None:
    """The water's mean over the `lag` hours to `k`, where at least half are known."""
    window = [h.water for h in hours[max(0, k - lag + 1) : k + 1] if h.water is not None]
    if len(window) * 2 < lag or not window:
        return None
    return sum(window) / len(window)


def usable(hours: Sequence[Hour], k: int) -> bool:
    h = hours[k]
    return not h.skip and h.room is not None and h.outdoor is not None


def simulate(hours: Sequence[Hour], k: int, f: Fit, ahead: int = AHEAD) -> float | None:
    """The room `ahead` hours after `k`, from the room at `k` and the measured inputs."""
    if not usable(hours, k):
        return None
    water = lagged(hours, k, f.lag)
    room = hours[k].room
    if water is None or room is None:
        return None
    mass = f.mass(room, hours[k], water)
    if mass is None:
        return None
    for i in range(k + 1, k + ahead + 1):
        if i >= len(hours) or hours[i].t - hours[i - 1].t != 3600 or not usable(hours, i):
            return None
        mass += f.r * (room - mass)
        water = lagged(hours, i, f.lag)
        if water is None:
            return None
        room = f.room(mass, hours[i], water)
    return room


def _starts(hours: Sequence[Hour], every: int = 2) -> list[int]:
    return [k for k in range(0, len(hours) - AHEAD, every) if usable(hours, k + AHEAD)]


def _error(hours: Sequence[Hour], f: Fit, starts: Sequence[int]) -> tuple[float, int]:
    misses = []
    for k in starts:
        found = simulate(hours, k, f)
        then = hours[k + AHEAD].room
        if found is not None and then is not None:
            misses.append(abs(found - then))
    return (sum(misses) / len(misses), len(misses)) if misses else (math.inf, 0)


def minimize(
    cost: Callable[[list[float]], float], start: list[float], steps: int = 600
) -> list[float]:
    """Nelder and Mead's simplex search: small, needs no gradient."""
    n = len(start)
    simplex = [list(start)]
    for i in range(n):
        point = list(start)
        point[i] = point[i] * 1.3 if point[i] else 0.05
        simplex.append(point)
    scores = [cost(p) for p in simplex]
    for _ in range(steps):
        order = sorted(range(n + 1), key=lambda i: scores[i])
        simplex = [simplex[i] for i in order]
        scores = [scores[i] for i in order]
        if abs(scores[-1] - scores[0]) < 1e-7:
            break
        center = [sum(p[j] for p in simplex[:-1]) / n for j in range(n)]
        worst = simplex[-1]
        reflected = [c + (c - w) for c, w in zip(center, worst, strict=True)]
        r = cost(reflected)
        if r < scores[0]:
            expanded = [c + 2 * (c - w) for c, w in zip(center, worst, strict=True)]
            e = cost(expanded)
            simplex[-1], scores[-1] = (expanded, e) if e < r else (reflected, r)
        elif r < scores[-2]:
            simplex[-1], scores[-1] = reflected, r
        else:
            contracted = [c + 0.5 * (w - c) for c, w in zip(center, worst, strict=True)]
            c_score = cost(contracted)
            if c_score < scores[-1]:
                simplex[-1], scores[-1] = contracted, c_score
            else:
                best = simplex[0]
                simplex = [best] + [
                    [b + 0.5 * (p - b) for b, p in zip(best, q, strict=True)] for q in simplex[1:]
                ]
                scores = [scores[0]] + [cost(p) for p in simplex[1:]]
    return simplex[min(range(n + 1), key=lambda i: scores[i])]


def _fit_lag(
    hours: Sequence[Hour],
    lag: int,
    starts: Sequence[int],
    household: bool = False,
    begin: Fit | None = None,
    searches: int = 1,
) -> Fit | None:
    """The best fit at `lag`: searched from `begin` where given, and begun again from where
    each search stopped, at most `searches` times."""
    has_sun = any(h.sun for h in hours)
    has_wind = any(h.wind for h in hours)

    def build(x: list[float]) -> Fit | None:
        alpha, beta, sigma, omega, gamma, r = x[:6]
        eta = x[6] if household else 0.0
        if min(alpha, beta, r, eta) < 0 or (has_sun and sigma < 0) or (has_wind and omega < 0):
            return None
        if alpha + beta >= 0.9 or r > 1:
            return None
        return Fit(
            alpha,
            beta,
            sigma if has_sun else 0.0,
            omega if has_wind else 0.0,
            gamma,
            r,
            lag,
            0,
            0.0,
            eta,
            household,
        )

    def cost(x: list[float]) -> float:
        f = build(x)
        if f is None:
            return 1e9
        error, n = _error(hours, f, starts)
        return error if n else 1e9

    if begin is not None:
        x = [begin.alpha, begin.beta, begin.sigma, begin.omega, begin.gamma, begin.r]
        x += [begin.eta] if household else []
    else:
        x = [0.2, 0.1, 0.01, 0.01, 0.0, 0.1] + ([0.05] if household else [])
    best = cost(x)
    for _ in range(searches):
        x = minimize(cost, x)
        found = cost(x)
        if found > best - 1e-6:
            break
        best = found
    f = build(x)
    if f is None:
        return None
    error, n = _error(hours, f, starts)
    return replace(f, rows=n, error=error)


def told_apart(hours: Sequence[Hour]) -> float:
    """How closely the heating (the water over the room) and the loss (the room over the
    outdoor temperature) moved together: near 1, as one."""
    x, y = [], []
    for h in hours:
        if h.skip or h.room is None or h.outdoor is None or h.water is None:
            continue
        x.append(h.water - h.room)
        y.append(h.room - h.outdoor)
    if len(x) < 3 or statistics.pstdev(x) == 0 or statistics.pstdev(y) == 0:
        return 1.0
    return abs(statistics.correlation(x, y))


Found = tuple[Fit | None, str | None, dict[str, float | int | str]]


def labeled(hours: Sequence[Hour]) -> list[Hour]:
    """The hours for the household's model: one whose occupancy isn't known is left out."""
    return [h if h.home is not None or h.skip else replace(h, skip="unknown") for h in hours]


def away_hours(hours: Sequence[Hour]) -> int:
    """Usable hours with time away in them."""
    return sum(
        1 for k, h in enumerate(hours) if usable(hours, k) and h.home is not None and h.home < 1
    )


def fit(hours: Sequence[Hour], household: bool = False) -> Found:
    """The best fit over the lags, or None and why. With `household`, the household's heat
    is a term, learned only with time away among the hours (`labeled` first)."""
    if household and (away := away_hours(hours)) < AWAY_LEAST:
        return None, NO_AWAY, {"hours": away, "least": AWAY_LEAST}
    rooms = [h.room for h in hours if not h.skip and h.room is not None]
    apart = [
        h.room - h.outdoor
        for h in hours
        if not h.skip and h.room is not None and h.outdoor is not None
    ]
    starts = _starts(hours)
    if len(starts) * 2 < LEAST_HOURS:
        return None, FEW, {"hours": len(starts) * 2, "least": LEAST_HOURS}
    if max(apart) - min(apart) < LEAST_SPREAD:
        return None, NARROW, {"spread": round(max(apart) - min(apart), 1)}
    if max(rooms) - min(rooms) < LEAST_MOVED:
        return None, STILL, {}
    if told_apart(hours) > TOLD_APART:
        return None, TOGETHER, {}
    tried = [f for lag in LAGS if (f := _fit_lag(hours, lag, starts, household)) and f.rows]
    if not tried:
        return None, FEW, {"hours": len(starts) * 2, "least": LEAST_HOURS}
    # The lags that followed best, searched on from where they stopped.
    tried.sort(key=lambda f: f.error)
    for f in tried[:REFINED]:
        again = _fit_lag(hours, f.lag, starts, household, f, SEARCHES)
        if again is not None and again.rows:
            tried.append(again)
    best = min(tried, key=lambda f: f.error)
    tau = best.tau_h
    if tau is None or not TAU_H[0] <= tau <= TAU_H[1]:
        return None, IMPLAUSIBLE, {"tau": round(tau) if tau is not None else 0}
    settled = best.settled
    if settled is not None and settled > HOUSEHOLD_MOST_C:
        return None, IMPLAUSIBLE_HOUSEHOLD, {"settled": round(settled, 1)}
    return best, None, {}


Judged = tuple[Fit | None, Verdict | None, str | None, dict[str, float | int | str]]


def judge(hours: Sequence[Hour], test_from: float) -> Judged:
    """Fit on the hours before `test_from`, and test on those after; then fit on all."""
    return _judged(hours, test_from)[0]


def _judged(hours: Sequence[Hour], test_from: float) -> tuple[Judged, Fit | None]:
    """`judge`, and the fit it tested."""
    train = [h for h in hours if h.t < test_from]
    trained, why, args = fit(train)
    verdict = None
    if trained is not None:
        predicted, baseline, actual = [], [], []
        for k, h in enumerate(hours):
            if h.t < test_from or k + AHEAD >= len(hours):
                continue
            then = hours[k + AHEAD]
            p = simulate(hours, k, trained)
            if p is None or then.room is None or then.skip:
                continue
            predicted.append(p)
            baseline.append(h.room)
            actual.append(then.room)
        verdict = held_out(predicted, baseline, actual)
    whole, why_all, args_all = fit(hours)
    if whole is None:
        return (None, verdict, why_all or why, args_all or args), trained
    return (whole, verdict, None, {}), trained


def contest(hours: Sequence[Hour], test_from: float) -> tuple[Judged, Judged]:
    """The model as it is, judged as `judge` judges it; and the household's, fitted on the
    hours whose occupancy is known before `test_from` and judged on the same held-out hours
    against both a simple guess and the model as it is. It is proved only where it beats
    both, the model as it is by the margin, with time away among those hours: home and away
    are given apart."""
    current, trained = _judged(hours, test_from)
    known = labeled(hours)
    challenger, why, args = fit([h for h in known if h.t < test_from], household=True)
    verdict = None
    if challenger is not None:
        verdict = _versus(hours, known, test_from, challenger, trained)
    whole, why_all, args_all = fit(known, household=True)
    if whole is None:
        return current, (None, verdict, why_all or why, args_all or args)
    return current, (whole, verdict, None, {})


def _versus(
    hours: Sequence[Hour],
    known: Sequence[Hour],
    test_from: float,
    challenger: Fit,
    current: Fit | None,
) -> Verdict:
    rows: list[tuple[float, float | None, float, float, bool]] = []
    """Each held-out comparison: the household's, the model as it is, the guess, what the
    room did, and whether there was time away in it."""
    for k, h in enumerate(known):
        if h.t < test_from or k + AHEAD >= len(known):
            continue
        then = known[k + AHEAD]
        p = simulate(known, k, challenger)
        if p is None or then.room is None or then.skip or h.room is None:
            continue
        c = simulate(hours, k, current) if current is not None else None
        if current is not None and c is None:
            continue
        window = known[k + 1 : k + AHEAD + 1]
        away = any(x.home is not None and x.home < 1 for x in window)
        rows.append((p, c, h.room, then.room, away))
    tested = sum(1 for r in rows if r[4])
    if len(rows) >= LEAST and tested < AWAY_TESTED:
        return Verdict(False, None, None, len(rows), FEW_AWAY, {"n": tested, "least": AWAY_TESTED})
    verdict = held_out([r[0] for r in rows], [r[2] for r in rows], [r[3] for r in rows])
    if not verdict.proved or current is None:
        return verdict

    def miss(i: int, away: bool | None = None) -> float | None:
        found = [abs((r[i] or 0.0) - r[3]) for r in rows if away is None or r[4] == away]
        return sum(found) / len(found) if found else None

    error, before = miss(0) or 0.0, miss(1) or 0.0
    args: dict[str, float | int | str] = {
        "error": round(error, 3),
        "current": round(before, 3),
        "away": round(miss(0, True) or 0.0, 3),
        "away_current": round(miss(1, True) or 0.0, 3),
        "settled": round(challenger.settled or 0.0, 2),
    }
    home, home_current = miss(0, False), miss(1, False)
    if home is not None and home_current is not None:
        args |= {"home": round(home, 3), "home_current": round(home_current, 3)}
    better = error <= before * (1 - MARGIN)
    if "home" in args:
        why = OVER if better else NOT_OVER
    else:
        why = OVER_AWAY if better else NOT_OVER_AWAY
    return Verdict(better, error, before, len(rows), why, args)
