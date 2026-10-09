"""Day-ahead prices for the plant: a plausible daily shape, seeded noise, and the extremes
the behavior contracts ask about."""

import random
from datetime import datetime, timedelta

from thermaestro.planner import Price
from thermaestro.planner.model import SLOT


def day_ahead(
    seed: int,
    start: datetime,
    days: float,
    *,
    spike: float = 1.0,
    negative: bool = False,
) -> list[Price]:
    """Prices from a day before `start` to a day after `days`, per 15 minutes: cheap
    nights, a morning and a dearer evening peak. `spike` multiplies the evening peak;
    `negative` takes the nights below zero."""
    rng = random.Random(seed)
    first = start - timedelta(days=1)
    first -= timedelta(seconds=first.timestamp() % SLOT.total_seconds())
    out = []
    t = first
    while t < start + timedelta(days=days + 1):
        hour = t.hour + t.minute / 60
        value = 1.0 + rng.gauss(0, 0.05)
        if 6 <= hour < 9:
            value += 0.5
        if 16 <= hour < 20:
            value += 0.8 * spike
        if hour < 5:
            value -= 1.6 if negative else 0.4
        out.append(Price(t, round(value, 4)))
        t += SLOT
    return out
