"""Day one: intents seeded from how the house runs now, so Thermaestro starts out wanting
what the pump alone gives. Each is a default (not confirmed) until the household confirms
or changes it; a part of the house that already has an intent of the kind isn't seeded.

- Each climate system: a level from its rooms' mean temperature over the last week,
  ±0.5 °C, and a comfort band at that level all the time; without a room sensor, normal
  heat that price may shift a little.
- The tank: a level at the device's stop temperature and a floor at its start
  temperature, both at the top; where the device doesn't say them, the top's usual daily
  high and low.
- A pool: a level between its start and stop temperatures, or its usual low and high.
- The addition: the pump's own logic.

Nothing here sets a cost stance or a power limit: those are the household's to set.
"""

import re
import statistics
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime

from . import kinds
from .model import Intent, Level

SEED = "seed"
BAND_HALF = 0.5


@dataclass(frozen=True)
class Found:
    """What the house shows now, for seeding."""

    systems: tuple[str, ...] = ()
    rooms: Mapping[str, str] = field(default_factory=dict)
    """Room scope to its climate system's."""
    room_means: Mapping[str, float] = field(default_factory=dict)
    """A room's mean temperature over the last week."""
    tanks: Mapping[str, tuple[float, float]] = field(default_factory=dict)
    """A tank's start and stop temperatures, or its top's usual daily low and high."""
    pools: Mapping[str, tuple[float, float]] = field(default_factory=dict)
    """A pool's start and stop temperatures, or its usual daily low and high."""
    addition: bool = False


def level_id(scope: str) -> str:
    return "current-" + re.sub(r"[^a-z0-9]+", "-", scope.lower()).strip("-")[:56]


def seed(
    found: Found, existing: list[Intent], now: datetime, name: str = "Current"
) -> tuple[list[Level], list[Intent]]:
    """The levels and default intents to add; none for what is already there."""
    has = {(i.kind, i.scope) for i in existing if i.open}
    levels: list[Level] = []
    intents: list[Intent] = []
    for system in found.systems:
        if ("comfort_band", system) in has:
            continue
        means = [m for room, m in found.room_means.items() if found.rooms.get(room) == system]
        if means:
            mean = round(statistics.fmean(means), 1)
            level = Level(
                id=level_id(system),
                name=name,
                scope=system,
                low=round(mean - BAND_HALF, 1),
                high=round(mean + BAND_HALF, 1),
            )
            levels.append(level)
            intents.append(
                kinds.comfort_band(
                    system,
                    [(level.id, ((), None, None))],
                    principal=SEED,
                    created=now,
                    confirmed=False,
                )
            )
        else:
            intents.append(
                kinds.no_sensor_band(system, principal=SEED, created=now, confirmed=False)
            )
    for tank, (low, high) in found.tanks.items():
        if ("hot_water_floor", tank) not in has:
            intents.append(
                kinds.hot_water_floor(
                    tank, _half_down(low), principal=SEED, created=now, confirmed=False
                )
            )
        levels.append(Level(id=level_id(tank), name=name, scope=tank, top=_half_down(high)))
    for pool, (low, high) in found.pools.items():
        if ("pool", pool) in has or high <= low:
            continue
        level = Level(
            id=level_id(pool), name=name, scope=pool, low=round(low, 1), high=round(high, 1)
        )
        levels.append(level)
        intents.append(kinds.pool(pool, level.id, principal=SEED, created=now, confirmed=False))
    if found.addition and ("addition_policy", "house") not in has:
        intents.append(kinds.addition_policy("pump", principal=SEED, created=now, confirmed=False))
    return levels, intents


def _half_down(value: float) -> float:
    return int(value * 2) / 2
