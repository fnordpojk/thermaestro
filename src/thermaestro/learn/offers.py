"""Hot-water times learned from the week's use, offered as standing intents. Never applied
without the household asking for one: an offer is only a suggestion until then.

An hour is offered when, on a weekday seen at least twice, it is where the day's use
peaks: at least `PEAK_LEAST` degrees of the tank drawn in it, a good share of the day's,
and no less than the hours either side. The same hour on several days is one offer; a
day whose hour a hot-water deadline already covers (within an hour) is left out, and an
offer the household turned down isn't made again for eight weeks.
"""

import hashlib
from collections.abc import Collection
from dataclasses import dataclass
from datetime import time
from typing import Any

from .tank import Demand

PEAK_LEAST = 1.0
"""Degrees of the tank drawn in an hour, at least, for a deadline to be worth offering."""
PEAK_SHARE = 0.25
"""Of the day's use, at least."""
SEEN_LEAST = 2
DISMISSED_S = 8 * 7 * 86_400.0


@dataclass(frozen=True, slots=True)
class Offer:
    scope: str
    by: time
    days: tuple[int, ...]
    drawn: float
    """Degrees of the tank drawn in the hour, the days' mean."""
    level: str | None = None
    temp: float | None = None

    @property
    def id(self) -> str:
        text = f"{self.scope}|{self.by:%H:%M}|{','.join(map(str, self.days))}"
        return hashlib.sha256(text.encode()).hexdigest()[:12]

    def request(self) -> dict[str, Any]:
        """As asked for over the API: a standing hot-water deadline."""
        deadline: dict[str, Any] = {"days": list(self.days), "by": f"{self.by:%H:%M}"}
        if self.level is not None:
            deadline["level"] = self.level
        else:
            deadline["temp"] = self.temp
        return {
            "kind": "hot_water_by",
            "scope": self.scope,
            "deadlines": [deadline],
            "strength": "should",
        }


def peaks(demand: Demand) -> dict[int, list[int]]:
    """Each weekday's peak hours, where it has been seen often enough."""
    out: dict[int, list[int]] = {}
    for d in range(7):
        if demand.seen[d] < SEEN_LEAST:
            continue
        hours = demand.day(d)
        total = sum(hours)
        found = [
            h
            for h, v in enumerate(hours)
            if v >= PEAK_LEAST
            and v >= PEAK_SHARE * total
            and v >= (hours[h - 1] if h > 0 else 0.0)
            and v >= (hours[h + 1] if h < 23 else 0.0)
        ]
        if found:
            out[d] = found
    return out


def offers(
    scope: str,
    demand: Demand,
    covered: Collection[tuple[int, int]],
    dismissed: Collection[str],
    *,
    level: str | None,
    temp: float | None,
) -> list[Offer]:
    """What to offer for a tank. `covered`: the (weekday, hour) a deadline already has."""
    by_hour: dict[int, list[int]] = {}
    for day, hours in peaks(demand).items():
        for h in hours:
            if not _near(covered, day, h):
                by_hour.setdefault(h, []).append(day)
    out = []
    for h, days in sorted(by_hour.items()):
        drawn = sum(demand.day(d)[h] for d in days) / len(days)
        offer = Offer(scope, time(h), tuple(sorted(days)), round(drawn, 2), level, temp)
        if offer.id not in dismissed:
            out.append(offer)
    return out


def _near(covered: Collection[tuple[int, int]], day: int, hour: int) -> bool:
    return any((day, h) in covered for h in (hour - 1, hour, hour + 1))
