import asyncio
from collections.abc import AsyncIterator
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from thermaestro.cap import Link, Message, Send, pair, serve
from thermaestro.cap.messages import (
    Describe,
    Described,
    Error,
    SeriesData,
    SeriesGet,
    SeriesSubscribe,
    SeriesUpdate,
)
from thermaestro.cap.model import Interval, Knowledge, Publication, SeriesInfo
from thermaestro.core.prices import assemble, slots
from thermaestro.core.series import Key, Series
from thermaestro.store import Database, PriceLayer, Vat

STOCKHOLM = ZoneInfo("Europe/Stockholm")
SPOT = SeriesInfo(
    id="se3/spot",
    kind="price",
    role="energy.spot",
    unit="SEK/kWh",
    vat="excl",
    resolution="PT15M",
    area="SE3",
    publication=Publication(daily_after="13:00", tz="Europe/Stockholm"),
)
TOTAL = SeriesInfo(
    id="home/price.total",
    kind="price",
    role="energy.supplier",
    covers=Knowledge(value=("energy.spot", "vat"), known="verified"),
    unit="SEK/kWh",
    vat="incl",
    resolution="PT15M",
)


class Clock:
    def __init__(self, at: datetime) -> None:
        self.now = at.timestamp()

    def __call__(self) -> float:
        return self.now


@pytest.fixture
async def db(tmp_path: Path) -> AsyncIterator[Database]:
    async with await Database.open(tmp_path / "t.db") as db:
        yield db


def quarters(series: str, start: datetime, n: int, value: float, **kw: object) -> list[Interval]:
    return [
        Interval.model_validate(
            {
                "series": series,
                "start": start + i * timedelta(minutes=15),
                "end": start + (i + 1) * timedelta(minutes=15),
                "value": value,
                "unit": "SEK/kWh",
                "vat": kw.get("vat", "excl"),
                "status": "final",
                "revision": kw.get("revision", 1),
            }
        )
        for i in range(n)
    ]


def local(y: int, m: int, d: int, h: int = 0, minute: int = 0) -> datetime:
    return datetime(y, m, d, h, minute, tzinfo=STOCKHOLM)


async def test_an_interval_keeps_its_latest_revision(db: Database) -> None:
    series = Series(db)
    key = Key("prices", "se3/spot")
    start = local(2026, 10, 7)
    await series.put("prices", quarters(key.series, start, 1, 0.5, revision=2))
    await series.put("prices", quarters(key.series, start, 1, 9.9, revision=1))  # older: ignored
    got = await series.get(key, start.timestamp(), (start + timedelta(hours=1)).timestamp())
    assert [(i.value, i.revision) for i in got] == [(0.5, 2)]
    await series.put("prices", quarters(key.series, start, 1, 0.6, revision=3))
    got = await series.get(key, start.timestamp(), (start + timedelta(hours=1)).timestamp())
    assert [(i.value, i.revision) for i in got] == [(0.6, 3)]


async def test_prices_go_stale_when_tomorrows_dont_come(db: Database) -> None:
    clock = Clock(local(2026, 10, 7, 12, 0))
    series = Series(db, clock)
    key = Key("prices", SPOT.id)
    series.describe("prices", [SPOT])
    assert series.freshness(key) == ("empty", None)
    await series.put("prices", quarters(SPOT.id, local(2026, 10, 7), 96, 0.5))
    assert series.freshness(key) == ("fresh", None)  # tomorrow's aren't due yet
    clock.now = local(2026, 10, 7, 15, 1).timestamp()
    assert series.freshness(key) == ("stale", "nothing for tomorrow yet, though due at 13:00")
    await series.put("prices", quarters(SPOT.id, local(2026, 10, 8), 96, 0.7))
    assert series.freshness(key) == ("fresh", None)


class SeriesPlugin:
    """A stand-in plugin offering one price series, with a revision on demand."""

    name = "prices"
    version = "0.1.0"
    features: tuple[str, ...] = ("subscribe",)

    def __init__(self) -> None:
        self.revise = asyncio.Event()
        self.start = local(2026, 10, 7)

    async def events(self, send: Send) -> None:
        await asyncio.Event().wait()

    async def handle(self, request: Message, send: Send) -> None:
        match request:
            case Describe():
                await send(Described(id=request.id, series=(SPOT,)))
            case SeriesGet():
                await send(
                    SeriesData(
                        id=request.id,
                        series=SPOT.id,
                        intervals=tuple(quarters(SPOT.id, self.start, 4, 0.5)),
                    )
                )
            case SeriesSubscribe():
                await self.revise.wait()
                await send(
                    SeriesUpdate(
                        id=request.id,
                        series=SPOT.id,
                        intervals=tuple(quarters(SPOT.id, self.start, 1, 0.55, revision=2)),
                    )
                )
                await asyncio.Event().wait()
            case _:
                await send(Error(id=getattr(request, "id", None), code="unsupported", detail="no"))


async def test_a_plugins_series_are_fetched_and_followed(db: Database) -> None:
    clock = Clock(local(2026, 10, 7, 10))
    series = Series(db, clock)
    plugin = SeriesPlugin()
    core, plugin_side = pair()
    served = asyncio.create_task(serve(plugin_side, plugin))
    heard: list[Key] = []
    series.listeners.append(heard.append)
    async with Link(core) as link:
        await link.hello(timeout=5)
        described = await link.describe(timeout=5)
        following = asyncio.create_task(series.follow("prices", link, described.series))
        key = Key("prices", SPOT.id)
        async with asyncio.timeout(5):
            while not heard:
                await asyncio.sleep(0.01)
        start = plugin.start.timestamp()
        assert [i.value for i in await series.get(key, start, start + 3600)] == [0.5] * 4
        plugin.revise.set()
        async with asyncio.timeout(5):
            while len(heard) < 2:
                await asyncio.sleep(0.01)
        assert [i.value for i in await series.get(key, start, start + 3600)] == [
            0.55,
            0.5,
            0.5,
            0.5,
        ]
        following.cancel()
        await asyncio.gather(following, return_exceptions=True)
    await plugin_side.close()
    served.cancel()
    await asyncio.gather(served, return_exceptions=True)


@pytest.mark.parametrize(
    ("day", "n"), [(date(2026, 3, 29), 92), (date(2026, 10, 25), 100), (date(2026, 10, 7), 96)]
)
def test_a_day_has_its_quarters(day: date, n: int) -> None:
    assert len(slots(day, STOCKHOLM)) == n


async def stocked(db: Database) -> Series:
    series = Series(db)
    series.describe("tibber", [SPOT, TOTAL])
    day = local(2026, 9, 29)
    await series.put("tibber", quarters(SPOT.id, day, 96, 0.59016))
    # Tibber's total: 1.25 times spot, plus its adders incl. VAT (0.1248).
    await series.put(
        "tibber", quarters(TOTAL.id, day, 96, round(1.25 * 0.59016 + 0.1248, 6), vat="incl")
    )
    return series


async def test_the_owners_se3_stack_from_its_parts(db: Database) -> None:
    series = await stocked(db)
    layers = {
        "spot": PriceLayer(
            role="energy.spot",
            source="series",
            plugin="tibber",
            series=SPOT.id,
            unit="SEK/kWh",
            vat="excl",
        ),
        "supplier": PriceLayer(
            role="energy.supplier", source="fixed", value=0.0998, unit="SEK/kWh", vat="excl"
        ),
        "energy-tax": PriceLayer(
            role="tax.energy", source="fixed", value=0.360, unit="SEK/kWh", vat="excl"
        ),
        "transfer": PriceLayer(
            role="grid.transfer", source="fixed", value=0.3116, unit="SEK/kWh", vat="excl"
        ),
    }
    vat = Vat(rate=0.25, applies_to=("spot", "supplier", "energy-tax", "transfer"))
    stack = await assemble(layers, vat, series, date(2026, 9, 29), STOCKHOLM)
    assert stack.problems == []
    assert stack.warnings == []
    assert stack.unit == "SEK/kWh"
    assert len(stack.slots) == 96
    assert stack.slots[0].total == pytest.approx(1.702, abs=0.001)
    # Tibber's total covers the spot price, the supplier's adders and VAT on both.
    [checked] = stack.checks
    assert (checked.series, checked.layers) == ("tibber:home/price.total", ["spot", "supplier"])
    assert (checked.compared, checked.differing) == (96, 0)


async def test_the_owners_se3_stack_from_tibbers_total(db: Database) -> None:
    series = await stocked(db)
    layers = {
        "tibber": PriceLayer(
            role="energy.supplier",
            source="series",
            plugin="tibber",
            series=TOTAL.id,
            unit="SEK/kWh",
            vat="incl",
        ),
        "energy-tax": PriceLayer(
            role="tax.energy", source="fixed", value=0.45, unit="SEK/kWh", vat="incl"
        ),
        "transfer": PriceLayer(
            role="grid.transfer", source="fixed", value=0.3895, unit="SEK/kWh", vat="incl"
        ),
    }
    stack = await assemble(
        layers, Vat(rate=0.25, applies_to=()), series, date(2026, 9, 29), STOCKHOLM
    )
    assert stack.problems == []
    assert stack.slots[0].total == pytest.approx(1.702, abs=0.001)  # the same arithmetic


async def test_a_stack_counted_wrong_is_refused(db: Database) -> None:
    series = await stocked(db)
    spot = PriceLayer(
        role="energy.spot",
        source="series",
        plugin="tibber",
        series=SPOT.id,
        unit="SEK/kWh",
        vat="excl",
    )
    total = PriceLayer(
        role="energy.supplier",
        source="series",
        plugin="tibber",
        series=TOTAL.id,
        unit="SEK/kWh",
        vat="incl",
    )
    twice = await assemble(
        {"spot": spot, "tibber": total}, None, series, date(2026, 9, 29), STOCKHOLM
    )
    assert twice.problems == ["energy.spot is counted twice: in spot and in tibber"]
    assert twice.slots == []
    on_vat = await assemble(
        {"tibber": total},
        Vat(rate=0.25, applies_to=("tibber",)),
        series,
        date(2026, 9, 29),
        STOCKHOLM,
    )
    assert on_vat.problems == ["VAT would be charged on tibber, which already includes it"]
    euros = PriceLayer(role="tax.energy", source="fixed", value=0.03, unit="EUR/kWh", vat="excl")
    mixed = await assemble({"spot": spot, "tax": euros}, None, series, date(2026, 9, 29), STOCKHOLM)
    assert mixed.problems == ["the layers are in different units: EUR/kWh, SEK/kWh"]


async def test_a_missing_layer_is_a_warning_and_a_gap_shows(db: Database) -> None:
    series = await stocked(db)
    spot = PriceLayer(
        role="energy.spot",
        source="series",
        plugin="tibber",
        series=SPOT.id,
        unit="SEK/kWh",
        vat="excl",
    )
    stack = await assemble({"spot": spot}, None, series, date(2026, 9, 30), STOCKHOLM)
    assert "no layer for tax.energy" in stack.warnings
    assert "some layers exclude VAT, and no VAT is set" in stack.warnings
    assert all(s.total is None and s.missing == ["spot"] for s in stack.slots)  # no prices that day


OTHER_SPOT = SeriesInfo(
    id="spot",
    kind="price",
    role="energy.spot",
    covers=Knowledge(value=(), known="documented"),
    unit="SEK/kWh",
    vat="excl",
    resolution="PT15M",
)


def spot_layer(**kw: object) -> PriceLayer:
    return PriceLayer.model_validate(
        {
            "role": "energy.spot",
            "source": "series",
            "plugin": "tibber",
            "series": SPOT.id,
            "unit": "SEK/kWh",
            "vat": "excl",
            **kw,
        }
    )


async def test_a_fallback_stands_in_where_the_series_has_no_price(db: Database) -> None:
    series = await stocked(db)
    series.describe("entsoe", [OTHER_SPOT])
    # The other source has the 29th and the 30th; the first only the 29th.
    await series.put("entsoe", quarters("spot", local(2026, 9, 29), 192, 0.6))
    layers = {"spot": spot_layer(fallbacks=("entsoe:spot",))}
    first = await assemble(layers, None, series, date(2026, 9, 29), STOCKHOLM)
    assert first.problems == []
    assert {p.fallback for s in first.slots for p in s.parts} == {None}
    assert first.slots[0].total == pytest.approx(0.59016)
    assert first.checks == []  # a fallback is part of the stack, not a check on it
    second = await assemble(layers, None, series, date(2026, 9, 30), STOCKHOLM)
    assert all(s.total == pytest.approx(0.6) for s in second.slots)
    assert {p.fallback for s in second.slots for p in s.parts} == {"entsoe:spot"}


async def test_another_sources_spot_price_checks_the_first(db: Database) -> None:
    series = await stocked(db)
    series.describe("entsoe", [OTHER_SPOT])
    await series.put("entsoe", quarters("spot", local(2026, 9, 29), 48, 0.59))
    await series.put("entsoe", quarters("spot", local(2026, 9, 29, 12), 48, 0.6))
    stack = await assemble({"spot": spot_layer()}, None, series, date(2026, 9, 29), STOCKHOLM)
    [checked] = stack.checks
    # 0.59 is within 1 % of 0.59016; 0.6 isn't.
    assert (checked.series, checked.compared, checked.differing) == ("entsoe:spot", 96, 48)
    assert checked.at == local(2026, 9, 29, 12)


async def test_a_fallback_of_another_kind_is_refused(db: Database) -> None:
    series = await stocked(db)
    euros = OTHER_SPOT.model_copy(update={"unit": "EUR/kWh"})
    series.describe("entsoe", [euros])
    stack = await assemble(
        {"spot": spot_layer(fallbacks=("entsoe:spot", "nowhere:spot", f"tibber:{TOTAL.id}"))},
        None,
        series,
        date(2026, 9, 29),
        STOCKHOLM,
    )
    assert stack.problems == [
        "layer spot: entsoe:spot is in EUR/kWh, VAT excl; the layer in SEK/kWh, VAT excl",
        "layer spot: no series nowhere:spot to fall back on",
        "layer spot: tibber:home/price.total isn't the same price"
        " (energy.supplier, not energy.spot)",
    ]


async def test_a_stack_that_disagrees_with_tibber_says_so(db: Database) -> None:
    series = await stocked(db)
    layers = {
        "spot": spot_layer(),
        # VAT forgotten on the supplier's adder: 0.0998 instead of 0.1248 with VAT.
        "supplier": PriceLayer(
            role="energy.supplier", source="fixed", value=0.0998, unit="SEK/kWh", vat="incl"
        ),
    }
    stack = await assemble(
        layers, Vat(rate=0.25, applies_to=("spot",)), series, date(2026, 9, 29), STOCKHOLM
    )
    [checked] = stack.checks
    assert (checked.compared, checked.differing) == (96, 96)
    assert checked.largest == pytest.approx(0.025, abs=0.0001)
    assert (
        "tibber:home/price.total differs from the stack's spot + supplier in 96 of 96 quarters,"
        " by up to 0.0250 SEK/kWh"
    ) in stack.warnings
