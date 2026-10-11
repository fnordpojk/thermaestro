"""The review of the pump's settings after NibePi: what is listed, and what is offered."""

from thermaestro.migrate import review
from thermaestro.migrate.review import Seen
from thermaestro.nibe.maps import load

F1245 = load("bus").model("F1245")


def test_only_what_the_pump_has_and_what_applies() -> None:
    listed = review.listed(F1245)
    assert listed[:2] == [47011, 47010]  # the heating offsets first
    assert 47134 in listed
    assert 48852 in listed
    assert not set(range(48659, 48665)) & set(listed)  # no cut-off bands on an F1245
    assert 49202 not in listed  # AXC40 models only
    # The one-time increase only while it is on.
    off = review.review(F1245, {48132: Seen("Off", 0)}, before={}, usual={}, levers={})
    on = review.review(F1245, {48132: Seen(4, 4)}, before={}, usual={}, levers={})
    assert 48132 not in [r.register for r in off]
    assert 48132 in [r.register for r in on]


def test_a_row_knows_its_default_before_and_lever() -> None:
    rows = review.review(
        F1245,
        {47134: Seen(0, 0), 47011: Seen(-2, -2), 47394: Seen(False, 0)},
        before={47134: 30},
        usual=review.offsets_by_register({1: 1.0}),
        levers=review.touched([("pump:hp1/cs1/heating.offset", ("x.nibe.47011",))]),
    )
    by = {r.register: r for r in rows}
    period = by[47134]
    assert (period.default, period.default_text, period.unit) == (60, "60", "min")
    assert (period.before, period.recommended, period.differs) == (30, True, True)
    assert period.point == "hp1/x.nibe.47134"
    offset = by[47011]
    assert (offset.usual, offset.lever, offset.differs) == (
        1.0,
        "pump:hp1/cs1/heating.offset",
        True,
    )
    assert (offset.low, offset.high, offset.step) == (-10, 10, 1)
    assert by[47394].default_text == "off"
    assert by[47394].differs is False
    assert by[48852].writable is False  # shown, never changed
    assert by[47375].now is None  # not read yet
    assert by[47375].differs is None


def test_the_full_comparison() -> None:
    comparable = review.comparable(F1245)
    assert len(comparable) > 300
    assert not set(comparable) & set(review.listed(F1245))
    assert all(F1245.register(r).writable for r in comparable)
    curve = 47007  # listed, so not compared
    assert curve not in comparable
    stop = 47376  # the addition's stop, factory 5.0 °C
    at_default = F1245.register(stop).default
    assert at_default is not None
    seen = {stop: Seen(at_default / 10 + 1, at_default + 10), 47212: Seen(None, None, "no answer")}
    rows = review.compared(F1245, seen, {})
    assert [r.register for r in rows] == [stop]
    assert rows[0].kind == "found"
