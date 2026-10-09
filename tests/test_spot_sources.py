"""The price sources that need no account, against stand-ins for their APIs: Energy-Charts,
Beneficial Apps' Nordic price sites, OMIE and Octopus Agile; and which of them a bidding
zone gets."""

import asyncio
import contextlib
import itertools
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from spotfake import CC_BY, PRIVATE, Fake, agile_rates, running

from thermaestro.cap import Link, pair, serve
from thermaestro.cap.conformance import run
from thermaestro.cap.messages import Health
from thermaestro.cap.model import Interval
from thermaestro.dayahead import DayAheadPlugin
from thermaestro.ecb import Rates
from thermaestro.energy_charts.plugin import EnergyChartsPlugin
from thermaestro.nordic_sites.plugin import NordicSitesPlugin
from thermaestro.octopus_agile.plugin import OctopusAgilePlugin
from thermaestro.omie.plugin import OmiePlugin
from thermaestro.spotsources import choices
from thermaestro.store import OctopusAgile, SpotZone

OSLO = ZoneInfo("Europe/Oslo")
STOCKHOLM = ZoneInfo("Europe/Stockholm")
HELSINKI = ZoneInfo("Europe/Helsinki")
MADRID = ZoneInfo("Europe/Madrid")
LONDON = ZoneInfo("Europe/London")
QUARTER = timedelta(minutes=15)
HOUR = timedelta(hours=1)
RATES = {date(2026, 10, 6): {"NOK": 10.8, "SEK": 11.2}, date(2026, 10, 7): {"NOK": 10.9}}


class Clock:
    def __init__(self, at: datetime) -> None:
        self.now = at.timestamp()

    def __call__(self) -> float:
        return self.now


@contextlib.asynccontextmanager
async def linked(p: DayAheadPlugin) -> AsyncIterator[tuple[Link, list[Health]]]:
    health: list[Health] = []
    core, plugin_side = pair()
    served = asyncio.create_task(serve(plugin_side, p))

    def heard(m: object) -> None:
        if isinstance(m, Health):
            health.append(m)

    try:
        async with Link(core, on_event=heard) as link:
            await link.hello(timeout=5)
            yield link, health
    finally:
        await plugin_side.close()
        served.cancel()
        await asyncio.gather(served, return_exceptions=True)


def fast(clock: Clock) -> dict[str, Any]:
    return {"clock": clock, "poll_s": 0.05, "health_interval_s": 0.05}


async def got(link: Link, series: str, start: datetime, days: int = 2) -> list[Interval]:
    data = await link.series_get(series, start, start + timedelta(days=days), timeout=5)
    return list(data.intervals)


async def first_health(health: list[Health]) -> Health:
    async with asyncio.timeout(5):
        while not health:
            await asyncio.sleep(0.01)
    return health[-1]


# --- Energy-Charts ---------------------------------------------------------------------------


async def test_energy_charts_says_a_zone_is_for_private_use() -> None:
    fake = Fake(rates=RATES)
    fake.ec[("NO1", date(2026, 10, 8))] = ("Europe/Oslo", QUARTER, 100.0)
    fake.ec[("NO1", date(2026, 10, 9))] = ("Europe/Oslo", QUARTER, 120.0)
    clock = Clock(datetime(2026, 10, 8, 14, tzinfo=OSLO))
    async with running(fake) as urls:
        p = EnergyChartsPlugin(
            SpotZone(zone="NO1", currency="NOK"),
            url=urls.energy_charts,
            rates=Rates(urls.ecb),
            **fast(clock),
        )
        async with linked(p) as (link, health):
            described = await link.describe(timeout=5)
            [info] = described.series
            assert (info.unit, info.resolution, info.area) == ("NOK/kWh", "PT15M", "NO1")
            terms = described.provider.terms if described.provider else None
            assert terms is not None
            assert (terms.license.value, terms.license.known) == (PRIVATE, "verified")
            assert any("private and internal use only" in c for c in terms.conditions.value or ())
            assert "ECB" in (terms.attribution.value or "")
            prices = await got(link, "spot", datetime(2026, 10, 8, tzinfo=OSLO))
            assert (await first_health(health)).stale == ()
    assert fake.asked[0] == "ec NO1 2026-10-08 2026-10-09"
    assert len(prices) == 192
    assert all(i.end - i.start == QUARTER for i in prices)
    # The 8th at the rate of the 6th, the latest out before the 7th's auction.
    assert prices[0].value == pytest.approx(0.1 * 10.8)
    assert prices[96].start == datetime(2026, 10, 9, tzinfo=OSLO)
    assert prices[96].value == pytest.approx(0.12 * 10.9)


async def test_energy_charts_a_cc_by_zone_and_an_hourly_one() -> None:
    fake = Fake()
    fake.ec_license = {"CH": CC_BY, "IT-Centre-North": PRIVATE}
    fake.ec[("CH", date(2026, 10, 8))] = ("Europe/Zurich", HOUR, 90.0)
    fake.ec[("IT-Centre-North", date(2026, 10, 8))] = ("Europe/Rome", QUARTER, 90.0)
    clock = Clock(datetime(2026, 10, 8, 10, tzinfo=ZoneInfo("Europe/Zurich")))
    async with running(fake) as urls:
        p = EnergyChartsPlugin(SpotZone(zone="CH"), url=urls.energy_charts, **fast(clock))
        async with linked(p) as (link, _):
            described = await link.describe(timeout=5)
            assert described.series[0].resolution == "PT1H"
            terms = described.provider.terms if described.provider else None
            assert terms is not None
            assert terms.license.value == CC_BY
            assert terms.conditions.value == ()
            assert "SMARD" in (terms.attribution.value or "")
            prices = await got(link, "spot", datetime(2026, 10, 8, tzinfo=UTC), days=1)
        assert len(prices) >= 22
        assert {i.end - i.start for i in prices} == {HOUR}
        p = EnergyChartsPlugin(SpotZone(zone="IT-CNOR"), url=urls.energy_charts, **fast(clock))
        async with linked(p) as (link, _):
            await link.describe(timeout=5)
    assert "ec IT-Centre-North 2026-10-08 2026-10-09" in fake.asked


async def test_energy_charts_with_nothing_published() -> None:
    fake = Fake()
    clock = Clock(datetime(2026, 10, 8, 10, tzinfo=OSLO))
    async with running(fake) as urls:
        p = EnergyChartsPlugin(SpotZone(zone="NO2"), url=urls.energy_charts, **fast(clock))
        async with linked(p) as (link, health):
            described = await link.describe(timeout=5)
            terms = described.provider.terms if described.provider else None
            assert terms is not None
            # Before any answer: the zone's license as the documentation lists it.
            assert (terms.license.value, terms.license.known) == (CC_BY, "documented")
            assert await got(link, "spot", datetime(2026, 10, 8, tzinfo=OSLO)) == []
            assert (await first_health(health)).state == "up"


# --- Beneficial Apps' Nordic price sites ------------------------------------------------------


async def test_a_swedish_site_per_quarter_in_kronor() -> None:
    fake = Fake(rates=RATES)
    fake.nordic["2026/10-08_SE3"] = ("Europe/Stockholm", QUARTER, 30.0)
    clock = Clock(datetime(2026, 10, 8, 10, tzinfo=STOCKHOLM))
    async with running(fake) as urls:
        p = NordicSitesPlugin(
            SpotZone(zone="SE3", currency="SEK"),
            base=urls.nordic,
            rates=Rates(urls.ecb),
            **fast(clock),
        )
        async with linked(p) as (link, _):
            described = await link.describe(timeout=5)
            assert described.provider is not None
            assert described.provider.name == "elprisetjustnu.se"
            assert "för vad som helst" in (described.provider.terms.license.value or "")
            assert described.series[0].resolution == "PT15M"
            prices = await got(link, "spot", datetime(2026, 10, 8, tzinfo=STOCKHOLM))
    assert fake.asked[:2] == ["nordic 2026/10-08_SE3.json", "nordic 2026/10-09_SE3.json"]
    assert len(prices) == 96  # tomorrow isn't out: a 404
    assert prices[1].value == pytest.approx(0.0305 * 11.2)
    assert prices[1].why == "converted from EUR at the ECB's rate of 2026-10-06: 11.2"


async def test_finland_is_one_zone_and_its_day_ends_first() -> None:
    fake = Fake()
    fake.nordic["2026/10-08"] = ("Europe/Helsinki", HOUR, 20.0)
    fake.nordic["2026/10-09"] = ("Europe/Helsinki", HOUR, 25.0)
    # 18:00 in Helsinki: tomorrow is out, up to Helsinki's midnight.
    clock = Clock(datetime(2026, 10, 8, 18, tzinfo=HELSINKI))
    async with running(fake) as urls:
        p = NordicSitesPlugin(SpotZone(zone="FI"), base=urls.nordic, **fast(clock))
        async with linked(p) as (link, health):
            described = await link.describe(timeout=5)
            assert described.series[0].resolution == "PT1H"
            prices = await got(link, "spot", datetime(2026, 10, 8, tzinfo=HELSINKI))
            assert (await first_health(health)).stale == ()
            assert p._holds_tomorrow()
    assert "nordic 2026/10-08.json" in fake.asked
    assert len(prices) == 48
    assert prices[-1].end == datetime(2026, 10, 10, tzinfo=HELSINKI)
    assert all(a.end == b.start for a, b in itertools.pairwise(prices))


async def test_no_site_outside_the_nordics() -> None:
    with pytest.raises(ValueError, match="no Beneficial Apps Nordic price site"):
        NordicSitesPlugin(SpotZone(zone="DE-LU"))


# --- OMIE --------------------------------------------------------------------------------------


@pytest.mark.parametrize(("zone", "first"), [("ES", 160.5), ("PT", 161.5)])
async def test_omie_spain_and_portugal(zone: str, first: float) -> None:
    fake = Fake()
    fake.omie[date(2026, 10, 9)] = (96, 160.0)
    clock = Clock(datetime(2026, 10, 9, 10, tzinfo=MADRID))
    async with running(fake) as urls:
        p = OmiePlugin(SpotZone(zone=zone), url=urls.omie, **fast(clock))
        async with linked(p) as (link, _):
            described = await link.describe(timeout=5)
            assert described.series[0].area == zone
            prices = await got(link, "spot", datetime(2026, 10, 9, tzinfo=MADRID), days=1)
    assert fake.asked[0] == "omie marginalpdbc_20261009.1"
    assert len(prices) == 96
    assert prices[0].start == datetime(2026, 10, 9, tzinfo=MADRID)
    assert prices[0].value == pytest.approx(first / 1000)


async def test_omie_when_the_clock_goes_back() -> None:
    fake = Fake()
    fake.omie[date(2026, 10, 25)] = (100, 50.0)
    clock = Clock(datetime(2026, 10, 25, 10, tzinfo=MADRID))
    async with running(fake) as urls:
        p = OmiePlugin(SpotZone(zone="ES"), url=urls.omie, **fast(clock))
        async with linked(p) as (link, _):
            await link.describe(timeout=5)
            prices = await got(link, "spot", datetime(2026, 10, 25, tzinfo=MADRID), days=1)
    assert len(prices) == 100
    assert prices[-1].end == datetime(2026, 10, 26, tzinfo=MADRID)


# --- Octopus Agile -----------------------------------------------------------------------------


async def test_octopus_agile_finds_the_product_and_holds_until_23() -> None:
    fake = Fake()
    fake.products = [
        {"code": "AGILE-OUTGOING-19-05-13", "available_from": "2019-05-13", "available_to": None},
        {"code": "AGILE-22-08-31", "available_from": "2022-08-31", "available_to": "2023-12-01"},
        {"code": "AGILE-24-10-01", "available_from": "2024-10-01", "available_to": None},
        {"code": "GO-VAR-22-10-14", "available_from": "2022-10-14", "available_to": None},
    ]
    tariff = "E-1R-AGILE-24-10-01-C"
    fake.octopus[tariff] = agile_rates(date(2026, 10, 9), 10.0) + agile_rates(
        date(2026, 10, 8), 20.0
    )
    clock = Clock(datetime(2026, 10, 8, 17, tzinfo=LONDON))
    async with running(fake) as urls:
        p = OctopusAgilePlugin(OctopusAgile(region="C"), url=urls.octopus, **fast(clock))
        async with linked(p) as (link, health):
            described = await link.describe(timeout=5)
            ids = {s.id: s for s in described.series}
            assert set(ids) == {"unit_rate", "unit_rate.excl"}
            assert (ids["unit_rate"].vat, ids["unit_rate"].unit) == ("incl", "GBP/kWh")
            assert ids["unit_rate"].resolution == "PT30M"
            rates = await got(link, "unit_rate", datetime(2026, 10, 7, 23, tzinfo=LONDON))
            assert (await first_health(health)).stale == ()
            assert p._holds_tomorrow()
    assert f"octopus {tariff}" in fake.asked
    assert len(rates) == 96
    assert rates[0].value == pytest.approx(0.20)
    assert rates[-1].end == datetime(2026, 10, 9, 23, tzinfo=LONDON)


# --- the plugins keep to the protocol ------------------------------------------------------


async def test_they_conform() -> None:
    today = datetime.now(OSLO).date()
    fake = Fake()
    for day in (today, today + timedelta(days=1)):
        fake.ec[("NO1", day)] = ("Europe/Oslo", QUARTER, 80.0)
        fake.nordic[f"{day:%Y/%m-%d}_NO1"] = ("Europe/Oslo", HOUR, 80.0)
        fake.omie[day] = (96, 80.0)
    async with running(fake) as urls:
        for p in (
            EnergyChartsPlugin(SpotZone(zone="NO1"), url=urls.energy_charts),
            NordicSitesPlugin(SpotZone(zone="NO1"), base=urls.nordic),
            OmiePlugin(SpotZone(zone="ES"), url=urls.omie),
        ):
            core, plugin_side = pair()
            served = asyncio.create_task(serve(plugin_side, p))
            try:
                assert list(await run(core, timeout_s=20, quiet_s=0.3)) == [], p.name
            finally:
                await plugin_side.close()
                served.cancel()
                await asyncio.gather(served, return_exceptions=True)


# --- which source a zone gets ----------------------------------------------------------------


def plugins(zone: str, **tokens: bool) -> list[tuple[str, bool]]:
    return [(c.plugin, c.private) for c in choices(zone, **tokens)]


def test_a_countrys_zones_share_a_source() -> None:
    assert plugins("SE1") == plugins("SE4") == [("nordic_sites", False)]
    assert plugins("NO1") == [("energy_charts", True), ("nordic_sites", False)]
    assert plugins("NO2") == [("energy_charts", False), ("nordic_sites", False)]
    assert plugins("DK1") == [("energy_charts", False), ("nordic_sites", False)]
    assert plugins("FI") == [("energy_charts", True), ("nordic_sites", False)]
    assert plugins("ES") == plugins("PT") == [("omie", False)]
    for zone in ("DE-LU", "AT", "BE", "NL", "FR", "CH", "PL", "CZ", "HU", "SI"):
        assert plugins(zone) == [("energy_charts", False)], zone
    # Italy: one zone is CC BY, the others aren't; the country takes one source.
    assert plugins("IT-NORD") == [("energy_charts", False)]
    assert plugins("IT-SICI") == [("energy_charts", True)]
    assert choices("XX") == []


def test_tokens_change_the_order() -> None:
    assert plugins("SE3", tibber=True, entsoe=True) == [
        ("tibber", False),
        ("nordic_sites", False),
        ("entsoe", False),
    ]
    assert plugins("FI", tibber=True) == [("energy_charts", True), ("nordic_sites", False)]
    # Where Energy-Charts is for private use only and nothing else covers the country.
    assert plugins("IT-SICI", entsoe=True) == [("entsoe", False), ("energy_charts", True)]
    assert plugins("NO1", entsoe=True)[-1] == ("entsoe", False)
    assert [c.token for c in choices("DE-LU", tibber=True)] == [True, False]
