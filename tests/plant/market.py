"""Day-ahead prices for the plant: a plausible daily shape, seeded noise, and the extremes
the behavior contracts ask about; and what a planner knows of them at a moment, as in a
market: the next day's from 13:00, and an estimate beyond, as the core makes it."""

import random
import statistics
from datetime import datetime, timedelta

from thermaestro.planner import Price
from thermaestro.planner.model import SLOT

PUBLISHED_AT = 13
"""The hour the next day's prices come out, in the house's time (UTC in the tests)."""
ESTIMATE_WEEKS = 4
HORIZON = timedelta(hours=48)


def _price(rng: random.Random, t: datetime, spike: float, negative: bool) -> Price:
    hour = t.hour + t.minute / 60
    value = 1.0 + rng.gauss(0, 0.05)
    if 6 <= hour < 9:
        value += 0.5
    if 16 <= hour < 20:
        value += 0.8 * spike
    if hour < 5:
        value -= 1.6 if negative else 0.4
    return Price(t, round(value, 4))


def day_ahead(
    seed: int,
    start: datetime,
    days: float,
    *,
    spike: float = 1.0,
    negative: bool = False,
    history_days: int = 0,
) -> list[Price]:
    """Prices from a day before `start` to a day after `days`, per 15 minutes: cheap
    nights, a morning and a more expensive evening peak. `spike` multiplies the evening peak;
    `negative` takes the nights below zero. `history_days` more before them, ordinary days,
    for an estimate to be made from; the prices from a day before `start` are the same
    with or without them."""
    first = start - timedelta(days=1)
    first -= timedelta(seconds=first.timestamp() % SLOT.total_seconds())
    out = []
    before = random.Random(seed + 1_000_003)
    t = first - timedelta(days=history_days)
    while t < first:
        out.append(_price(before, t, 1.0, False))
        t += SLOT
    rng = random.Random(seed)
    while t < start + timedelta(days=days + 1):
        out.append(_price(rng, t, spike, negative))
        t += SLOT
    return out


def known(series: list[Price], now: datetime) -> list[Price]:
    """What a planner knows at `now`: the prices published by then (yesterday's, today's, and
    the next day's from 13:00), and beyond them, to two days ahead, each slot's median over the same
    slot of the four weeks before, marked as estimated, as the core estimates them."""
    day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    until = day + timedelta(days=2 if now.hour >= PUBLISHED_AT else 1)
    values = {p.start: p.value for p in series}
    out = [p for p in series if day - timedelta(days=1) <= p.start < until]
    t = until
    while t < now + HORIZON:
        seen = [
            values[back]
            for k in range(1, ESTIMATE_WEEKS + 1)
            if (back := t - timedelta(weeks=k)) in values
        ]
        if seen:
            out.append(Price(t, round(statistics.median(seen), 4), estimated=True))
        t += SLOT
    return out
