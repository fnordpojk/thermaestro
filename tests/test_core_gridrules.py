"""Grid rules as the household enters them: when each part holds, a time-of-use rule's
price, and its layer in the price stack."""

from collections.abc import AsyncIterator
from datetime import date, time
from pathlib import Path

import pytest
from pydantic import ValidationError
from test_core_series import SPOT, STOCKHOLM, local, stocked

from thermaestro.core.gridrules import holds, tou_price
from thermaestro.core.prices import assemble
from thermaestro.intents.calendar import Calendar
from thermaestro.store import Database, GridRule, PriceLayer, Vat
from thermaestro.store.settings import Rate, When

SWEDISH = Calendar(STOCKHOLM, "SE").holiday
WINTER = (1, 2, 3, 11, 12)

TOU = GridRule(
    type="tou",
    owner="A grid company",
    unit="SEK/kWh",
    base=0.10,
    rates=(Rate(months=WINTER, days="working_days", start=time(7), end=time(20), price=0.50),),
)


@pytest.fixture
async def db(tmp_path: Path) -> AsyncIterator[Database]:
    async with await Database.open(tmp_path / "t.db") as opened:
        yield opened


def test_a_rate_holds_on_its_months_days_and_hours() -> None:
    assert tou_price(TOU, local(2026, 12, 21, 8), STOCKHOLM, SWEDISH) == 0.50  # a Monday
    assert tou_price(TOU, local(2026, 12, 21, 20), STOCKHOLM, SWEDISH) == 0.10  # its end
    assert tou_price(TOU, local(2026, 12, 19, 8), STOCKHOLM, SWEDISH) == 0.10  # a Saturday
    assert tou_price(TOU, local(2026, 6, 15, 8), STOCKHOLM, SWEDISH) == 0.10  # summer
    # Christmas Day is a Friday and a public holiday: not a working day.
    assert tou_price(TOU, local(2026, 12, 25, 8), STOCKHOLM, SWEDISH) == 0.10
    assert tou_price(TOU, local(2026, 12, 25, 8), STOCKHOLM) == 0.50  # without the holidays


def test_past_midnight_and_on_normal_time() -> None:
    night = When(start=time(22), end=time(6))
    assert holds(night, local(2026, 10, 10, 23))
    assert holds(night, local(2026, 10, 11, 5))
    assert not holds(night, local(2026, 10, 10, 12))
    day = Rate(start=time(7), end=time(20), price=0.50)
    normal = TOU.model_copy(update={"clock": "normal", "rates": (day,)})
    # In summer, 07:30 civil time is 06:30 normal time: not yet.
    assert tou_price(normal, local(2026, 6, 15, 7, 30), STOCKHOLM) == 0.10
    assert tou_price(normal, local(2026, 6, 15, 8, 30), STOCKHOLM) == 0.50
    assert tou_price(normal, local(2026, 12, 15, 7, 30), STOCKHOLM) == 0.50


def test_a_rule_not_in_force() -> None:
    paused = TOU.model_copy(update={"status": "paused"})
    assert tou_price(paused, local(2026, 12, 21, 8), STOCKHOLM) == 0.0  # charges nothing
    later = TOU.model_copy(update={"valid_from": date(2027, 1, 1)})
    assert tou_price(later, local(2026, 12, 21, 8), STOCKHOLM) is None  # not known then


def test_what_a_rule_says() -> None:
    with pytest.raises(ValidationError, match="gives its base"):
        GridRule(type="tou", owner="x", unit="SEK/kWh")
    GridRule(type="tou", owner="x", unit="SEK/kWh", unknown=("base",))
    with pytest.raises(ValidationError, match="has no kw"):
        GridRule(type="tou", owner="x", unit="SEK/kWh", base=0.1, kw=16)
    with pytest.raises(ValidationError, match="gives their unit"):
        GridRule(type="interval_peak", owner="x", unknown=("interval_minutes", "peaks",
                 "different_days", "price_per_kw"))  # fmt: skip
    # A power charge announced and paused, as far as the grid company has said.
    GridRule(
        type="interval_peak",
        owner="A grid company",
        status="paused",
        unit="SEK/kW",
        window=(When(months=WINTER, days="working_days", start=time(6), end=time(21)),),
        unknown=("interval_minutes", "peaks", "different_days", "price_per_kw"),
    )
    GridRule(type="subscribed_power", owner="x", kw=16)


SPOT_LAYER = PriceLayer(
    role="energy.spot", source="series", plugin="tibber", series=SPOT.id, unit="SEK/kWh", vat="excl"
)


async def test_a_time_of_use_rule_is_a_layer_of_the_stack(db: Database) -> None:
    series = await stocked(db)  # spot 0.59016 every quarter of 2026-09-29, a Tuesday
    all_year = TOU.model_copy(update={"rates": (TOU.rates[0].model_copy(update={"months": ()}),)})
    layers = {
        "spot": SPOT_LAYER,
        "grid": PriceLayer(role="grid.tou", source="rule", rule="grid", unit="SEK/kWh", vat="excl"),
    }
    vat = Vat(rate=0.25, applies_to=("spot", "grid"))
    stack = await assemble(
        layers, vat, series, date(2026, 9, 29), STOCKHOLM, {"grid": all_year}, SWEDISH
    )
    assert stack.problems == []
    by_hour = {s.start.astimezone(STOCKHOLM).hour: s.total for s in stack.slots}
    assert by_hour[3] == pytest.approx(1.25 * (0.59016 + 0.10))
    assert by_hour[8] == pytest.approx(1.25 * (0.59016 + 0.50))


async def test_a_rule_layer_that_cant_be_used_is_refused(db: Database) -> None:
    series = await stocked(db)
    layer = PriceLayer(role="grid.tou", source="rule", rule="grid", unit="SEK/kWh", vat="excl")
    day = date(2026, 9, 29)
    trip = GridRule(type="subscribed_power", owner="x", kw=16)
    euros = TOU.model_copy(update={"unit": "EUR/kWh"})
    for rules, why in (
        ({}, "there is no grid rule 'grid'"),
        ({"grid": trip}, "has no prices per kWh"),
        ({"grid": euros}, "is in EUR/kWh, the layer in SEK/kWh"),
    ):
        stack = await assemble({"grid": layer}, None, series, day, STOCKHOLM, rules)
        assert any(why in p for p in stack.problems), stack.problems
        assert stack.slots == []
