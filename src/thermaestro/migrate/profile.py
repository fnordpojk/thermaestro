"""NibePi's learned hot-water profile (`vv_ai_profile.json`), as a starting point for the
tank's week.

The file is `{profile, tempPlan, modePlan, meta}`: `profile` is 168 numbers, degrees the
charge sensor fell in each hour of the week, Monday 00:00 first, in the local time of
NibePi's process (in a container without a time zone set, usually UTC). Only `profile` is
read: the plans are made from it.

The charge sensor sits low in the tank, so a fall there is about half a fall of the
tank's mean: the hours are halved into degrees of the tank, and moved from NibePi's time
zone into the house's. The week starts from them as if each weekday had been seen
`SEEN` times, so what Thermaestro learns itself takes over within weeks.
"""

import json
from datetime import date, datetime, time, timedelta, tzinfo

from ..learn.tank import HOURS, Demand

SEEN = 4
CHARGE_TO_TANK = 0.5


class NotProfile(ValueError):
    pass


def read(text: str) -> list[float]:
    """The 168 hours of a `vv_ai_profile.json`."""
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise NotProfile(f"not JSON: {e.msg}") from None
    profile = data.get("profile") if isinstance(data, dict) else None
    if not isinstance(profile, list) or len(profile) != HOURS:
        raise NotProfile("not a NibePi hot-water profile: it needs 168 hours under profile")
    out = []
    for value in profile:
        if not isinstance(value, int | float) or isinstance(value, bool) or value < 0:
            raise NotProfile("the profile's hours must be numbers, none below 0")
        out.append(float(value))
    return out


def week(profile: list[float], kept_in: tzinfo, house: tzinfo, around: date) -> Demand:
    """The profile as the tank's week in the house's time zone; `around` picks the week whose
    offsets between the two zones are used."""
    monday = around - timedelta(days=around.weekday())
    hours = [0.0] * HOURS
    for i, value in enumerate(profile):
        day, hour = divmod(i, 24)
        there = datetime.combine(monday + timedelta(days=day), time(hour), kept_in)
        here = there.astimezone(house)
        hours[here.weekday() * 24 + here.hour] += value * CHARGE_TO_TANK
    return Demand(hours, [SEEN] * 7)
