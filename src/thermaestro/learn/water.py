"""The cold water's temperature through the year, estimated: no pump measures it.

Municipal water follows the seasons a month or two late; the correlation EnergyPlus uses
for it (Hendron et al., SimBuild 2004) gives it from the location's annual mean outdoor
temperature and the spread between its warmest and coldest months. A private well stays
near the year's mean. Unknown counts as municipal.

The same shower takes more heat from the tank when the water comes in colder, so what the
household draws is compared across seasons at the year's mean inlet temperature.
"""

import math
from datetime import date

USE_C = 40.0
"""About what a shower or a tap mixes to."""


def inlet(
    day: date, annual_mean: float, spread: float, *, well: bool, south: bool = False
) -> float:
    """The cold water's temperature on `day`, °C."""
    if well:
        return annual_mean
    mean_f = annual_mean * 1.8 + 32
    spread_f = spread * 1.8
    ratio = 0.4 + 0.01 * (mean_f - 44)
    lag = 35 - (mean_f - 44)
    n = day.timetuple().tm_yday + (182 if south else 0)
    degrees = 0.986 * (n - 15 - lag) - 90
    out_f = mean_f + 6 + ratio * spread_f / 2 * math.sin(math.radians(degrees))
    return (out_f - 32) / 1.8


def season(
    day: date, annual_mean: float, spread: float, *, well: bool, south: bool = False
) -> float:
    """How much more heat a draw takes on `day` than at the year's mean inlet temperature."""
    year_mean = inlet(day, annual_mean, 0.0, well=well, south=south)
    today = inlet(day, annual_mean, spread, well=well, south=south)
    return (USE_C - today) / (USE_C - year_mean)
