from datetime import UTC, datetime

from thermaestro.cap.model import Delivery, Envelope, Point
from thermaestro.core.plausible import Plausibility, standard_name

COUNTER = "hp1/heat.produced{purpose=dhw,by=total}"


def envelope(point: str, value: float | bool | str | None, unit: str | None) -> Envelope:
    now = datetime.now(UTC)
    return Envelope(
        point=point,
        value=value,
        unit=unit,
        t_observed=now,
        t_received=now,
        quality="good",
        source="measured",
    )


def test_standard_names() -> None:
    assert standard_name(COUNTER) == "heat.produced"
    assert standard_name("hp1/cs1/supply.temp") == "supply.temp"
    assert standard_name("room.living/temperature#2") == "temperature"


def test_implausible_values_are_kept_but_not_good() -> None:
    check = Plausibility()
    hot = check.check("pump", envelope("hp1/outdoor.temp", 900.0, "degC"), None)
    assert (hot.quality, hot.value) == ("out_of_range", 900.0)
    assert hot.why == "implausible: outside -60..150 degC"
    assert check.check("pump", envelope("hp1/outdoor.temp", -12.5, "degC"), None).quality == "good"
    assert check.check("x", envelope("x/humidity", 101.0, "%"), None).quality == "out_of_range"
    # Units it has no range for, text, on/off and values already marked pass untouched.
    assert check.check("x", envelope("x/thing", 1e12, "furlongs"), None).quality == "good"
    assert check.check("x", envelope("x/state", "running", None), None).quality == "good"
    assert check.check("x", envelope("x/on", True, "%"), None).quality == "good"


def test_a_counter_may_not_run_backwards() -> None:
    check = Plausibility()
    assert check.check("pump", envelope(COUNTER, 348.3, "kWh"), None).quality == "good"
    assert check.check("pump", envelope(COUNTER, 348.4, "kWh"), None).quality == "good"
    back = check.check("pump", envelope(COUNTER, 12.0, "kWh"), None)
    assert (back.quality, back.why) == ("out_of_range", "the counter ran backwards, from 348.4")
    # The last good reading stays the reference.
    assert check.check("pump", envelope(COUNTER, 348.5, "kWh"), None).quality == "good"
    # Another instance keeps its own count.
    assert check.check("other", envelope(COUNTER, 1.0, "kWh"), None).quality == "good"


def test_a_counter_that_wraps_starts_over() -> None:
    point = Point(
        path=COUNTER, unit="kWh", delivery=Delivery(how="polled", cost_s=1.0), wraps_at=6553.6
    )
    check = Plausibility()
    assert check.check("pump", envelope(COUNTER, 6553.2, "kWh"), point).quality == "good"
    wrapped = check.check("pump", envelope(COUNTER, 0.3, "kWh"), point)
    assert wrapped.quality == "good"
    # A small drop is still a counter running backwards, wrap or not.
    assert check.check("pump", envelope(COUNTER, 0.1, "kWh"), point).quality == "out_of_range"
