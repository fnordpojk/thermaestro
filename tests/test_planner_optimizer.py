"""The optimizer in the running core: how it reads what was measured."""

import pytest

from thermaestro.core.values import Sample
from thermaestro.planner.optimizer import held_mean


def good(t: float, value: float) -> Sample:
    return Sample(t, value, None, "good")


def test_a_reading_holds_until_the_next() -> None:
    samples = [good(0, 30.0), good(1800, 40.0)]
    assert held_mean(samples, 0, 3600) == pytest.approx(35.0)
    # Weighed by how long each held, not by how many there were.
    samples = [good(0, 30.0), good(3000, 40.0), good(3300, 40.0)]
    assert held_mean(samples, 0, 3600) == pytest.approx((30 * 3000 + 40 * 600) / 3600)


def test_a_reading_that_isnt_good_ends_the_one_before() -> None:
    """While a charge takes the water the supply has no flow: the heating's water before it
    isn't held through the charge, and the charge's own readings don't count."""
    samples = [
        good(0, 30.0),
        Sample(1200, 55.0, None, "no_flow"),
        Sample(1800, 56.0, None, "no_flow"),
        good(2400, 28.0),
    ]
    assert held_mean(samples, 0, 3600) == pytest.approx((30 * 1200 + 28 * 1200) / 2400)
    assert held_mean(samples[1:3], 1200, 2400) is None
