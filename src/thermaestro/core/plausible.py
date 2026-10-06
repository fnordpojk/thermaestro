"""Values no device should report, whatever it says.

A plugin decodes what its device sends and checks it against the device's own limits.
The core checks again, against what is physically plausible in a house, so a device that
reports nonsense (a meter it doesn't have, a flipped bit) can't feed the plan:
- each unit has a plausible range; a value outside it is `out_of_range`;
- a counter (an energy meter) may not run backwards. A drop of more than half the span
  its plugin declares (`wraps_at`) is the counter starting over from 0, and accepted.

A value that fails keeps its number, so it can be shown and investigated, but its
quality says it isn't to be used.
"""

import re
from dataclasses import dataclass, field

from ..cap.model import Envelope, Point

RANGES: dict[str, tuple[float, float]] = {
    "degC": (-60.0, 150.0),
    "\N{DEGREE SIGN}C": (-60.0, 150.0),
    "K": (-150.0, 150.0),
    "%": (0.0, 100.0),
    "kWh": (0.0, 1e9),
    "MWh": (0.0, 1e6),
    "kW": (-100.0, 100.0),
    "W": (-100_000.0, 100_000.0),
    "ppm": (0.0, 10_000.0),
    "hPa": (800.0, 1100.0),
    "lx": (0.0, 200_000.0),
    "W/m2": (0.0, 1500.0),
    "g/m3": (0.0, 100.0),
    "L/min": (0.0, 1000.0),
    "l/min": (0.0, 1000.0),
    "L": (0.0, 1e9),
    "m3/h": (0.0, 10_000.0),
    "Hz": (0.0, 200.0),
    "rpm": (0.0, 10_000.0),
    "bar": (-1.0, 50.0),
}
"""What a house can plausibly measure, by unit. Wider than any real installation, so a
true value never falls outside; only nonsense does."""

COUNTERS = ("heat.produced", "elec.used")
"""Standard names of points that only count up."""

_NAME = re.compile(r"([^/{#]+)(?:\{[^}]*\})?(?:#\d+)?$")


def standard_name(path: str) -> str:
    """The name in a point's path, without its node, qualifiers or suffix."""
    match = _NAME.search(path)
    return match.group(1) if match else path


@dataclass
class Plausibility:
    _last: dict[tuple[str, str], float] = field(default_factory=dict)
    """The last good reading of each counter, per instance and point."""

    def check(self, instance: str, envelope: Envelope, point: Point | None) -> Envelope:
        value = envelope.value
        if (
            envelope.quality != "good"
            or isinstance(value, bool)
            or not isinstance(value, int | float)
        ):
            return envelope
        limits = RANGES.get(envelope.unit or "")
        if limits is not None and not limits[0] <= value <= limits[1]:
            low, high = limits
            return _refused(envelope, f"implausible: outside {low:g}..{high:g} {envelope.unit}")
        if standard_name(envelope.point).startswith(COUNTERS):
            key = (instance, envelope.point)
            last = self._last.get(key)
            if last is not None and value < last:
                wraps_at = point.wraps_at if point is not None else None
                if wraps_at is None or last - value <= wraps_at / 2:
                    return _refused(envelope, f"the counter ran backwards, from {last:g}")
            self._last[key] = value
        return envelope


def _refused(envelope: Envelope, why: str) -> Envelope:
    return envelope.model_copy(update={"quality": "out_of_range", "why": why})
