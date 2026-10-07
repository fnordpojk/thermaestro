"""The ENTSO-E plugin against stand-ins for the platform's API and the ECB's rates."""

import asyncio
import contextlib
import itertools
from collections.abc import AsyncIterator
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from entsoefake import SE3, TOKEN, FakeEntsoE, Period, day, running

from thermaestro.cap import Link, pair, serve
from thermaestro.cap.conformance import run
from thermaestro.cap.messages import Health
from thermaestro.cap.model import Interval
from thermaestro.core.series import Key, Series
from thermaestro.entsoe.ecb import Rates
from thermaestro.entsoe.plugin import EntsoEPlugin
from thermaestro.store import Database, EntsoE, SecretStore

STOCKHOLM = ZoneInfo("Europe/Stockholm")


class Clock:
    def __init__(self, at: datetime) -> None:
        self.now = at.timestamp()

    def __call__(self) -> float:
        return self.now


def local(y: int, m: int, d: int, h: int = 0, zone: ZoneInfo = STOCKHOLM) -> datetime:
    return datetime(y, m, d, h, tzinfo=zone)


def rates() -> dict[date, dict[str, float]]:
    # Friday the 9th, Monday the 5th and Tuesday the 6th of October 2026.
    return {
        date(2026, 10, 5): {"SEK": 11.0, "NOK": 10.8},
        date(2026, 10, 6): {"SEK": 11.2, "NOK": 10.9},
        date(2026, 10, 9): {"SEK": 11.3, "NOK": 11.0},
    }


async def secrets(tmp_path: Path, token: str = TOKEN) -> SecretStore:
    store = SecretStore(tmp_path / "secrets.json")
    await store.set("entsoe.token", token)
    return store


@contextlib.asynccontextmanager
async def plugin(
    fake: FakeEntsoE, store: SecretStore, clock: Clock, **settings: Any
) -> AsyncIterator[tuple[EntsoEPlugin, Link, list[Health]]]:
    async with running(fake) as (url, ecb):
        p = EntsoEPlugin(
            EntsoE.model_validate({"token": "entsoe.token", "zone": "SE3", **settings}),
            secrets=store,
            url=url,
            rates=Rates(ecb),
            clock=clock,
            poll_s=0.05,
            health_interval_s=0.05,
        )
        health: list[Health] = []
        core, plugin_side = pair()
        served = asyncio.create_task(serve(plugin_side, p))

        def heard(m: object) -> None:
            if isinstance(m, Health):
                health.append(m)

        try:
            async with Link(core, on_event=heard) as link:
                await link.hello(timeout=5)
                yield p, link, health
        finally:
            await plugin_side.close()
            served.cancel()
            await asyncio.gather(served, return_exceptions=True)


async def spot(link: Link, start: datetime, days: int = 2) -> list[Interval]:
    data = await link.series_get("spot", start, start + timedelta(days=days), timeout=5)
    return list(data.intervals)


async def test_a_zone_in_kronor(tmp_path: Path) -> None:
    fake = FakeEntsoE([day(date(2026, 10, 7)), day(date(2026, 10, 8), base=80)], rates=rates())
    clock = Clock(local(2026, 10, 7, 14))
    async with plugin(fake, await secrets(tmp_path), clock, currency="SEK") as (_, link, _):
        described = await link.describe(timeout=5)
        [info] = described.series
        assert (info.id, info.role, info.unit, info.vat, info.area) == (
            "spot",
            "energy.spot",
            "SEK/kWh",
            "excl",
            "SE3",
        )
        assert described.provider is not None
        attribution = described.provider.terms.attribution.value or ""
        assert "ENTSO-E Transparency Platform" in attribution
        assert "ECB" in attribution
        got = await spot(link, local(2026, 10, 7))
    asked = fake.asked[0]
    assert (asked["documentType"], asked["in_Domain"], asked["out_Domain"]) == ("A44", SE3, SE3)
    assert (asked["periodStart"], asked["periodEnd"]) == ("202610062200", "202610082200")
    assert asked["contract_MarketAgreement.type"] == "A01"
    assert len(got) == 192
    first, fourth = got[0], got[3]
    # The 7th at Monday's rate, the latest out before the auction on the 6th.
    assert first.value == pytest.approx(50.0 / 1000 * 11.0)
    assert first.source == "calculated"
    assert first.why == "converted from EUR at the ECB's rate of 2026-10-05: 11.0"
    assert fourth.value == got[2].value  # left out of the A03 curve, held from the point before
    assert got[96].value == pytest.approx(80.0 / 1000 * 11.2)
    assert got[96].start == local(2026, 10, 8)
    assert got[-1].end == local(2026, 10, 9)


async def test_a_monday_takes_fridays_rate(tmp_path: Path) -> None:
    fake = FakeEntsoE([day(date(2026, 10, 12))], rates=rates())
    clock = Clock(local(2026, 10, 12, 10))
    async with plugin(fake, await secrets(tmp_path), clock, currency="SEK") as (_, link, _):
        await link.describe(timeout=5)
        got = await spot(link, local(2026, 10, 12), days=1)
    assert got[0].why == "converted from EUR at the ECB's rate of 2026-10-09: 11.3"


async def test_a_euro_zone_needs_no_rate(tmp_path: Path) -> None:
    helsinki = ZoneInfo("Europe/Helsinki")
    fake = FakeEntsoE([day(date(2026, 10, 7), zone="Europe/Helsinki")], eic="10YFI-1--------U")
    clock = Clock(local(2026, 10, 7, 10, helsinki))
    async with plugin(fake, await secrets(tmp_path), clock, zone="FI") as (_, link, _):
        described = await link.describe(timeout=5)
        assert described.series[0].unit == "EUR/kWh"
        got = await spot(link, local(2026, 10, 7, 0, helsinki), days=1)
    assert got[0].value == pytest.approx(0.05)
    assert got[0].source is None
    assert fake.rates_asked == 0


@pytest.mark.parametrize(("on", "n"), [(date(2026, 3, 29), 92), (date(2026, 10, 25), 100)])
async def test_days_that_change_the_clock(tmp_path: Path, on: date, n: int) -> None:
    fake = FakeEntsoE([day(on)])
    clock = Clock(datetime(on.year, on.month, on.day, 10, tzinfo=STOCKHOLM))
    async with plugin(fake, await secrets(tmp_path), clock, currency="EUR") as (_, link, _):
        await link.describe(timeout=5)
        start = datetime(on.year, on.month, on.day, tzinfo=STOCKHOLM)
        got = await spot(link, start, days=1)
    assert len(got) == n
    assert got[-1].end == start + timedelta(days=1)  # the next local midnight
    assert all(a.end == b.start for a, b in itertools.pairwise(got))


async def test_sequence_one_and_the_finest_resolution(tmp_path: Path) -> None:
    on = date(2026, 10, 7)
    berlin = ZoneInfo("Europe/Berlin")
    sdac = day(on, base=60, zone="Europe/Berlin", sequence=1)
    other = day(on, base=99, zone="Europe/Berlin", sequence=2)
    fake = FakeEntsoE([sdac, other], eic="10Y1001A1001A82H")
    clock = Clock(local(2026, 10, 7, 10, berlin))
    async with plugin(fake, await secrets(tmp_path), clock, zone="DE-LU") as (_, link, _):
        await link.describe(timeout=5)
        got = await spot(link, local(2026, 10, 7, 0, berlin), days=1)
    assert fake.asked[0]["classificationSequence_AttributeInstanceComponent.position"] == "1"
    assert got[0].value == pytest.approx(0.06)
    # France: an hourly series beside the quarter-hourly one; the quarters are used.
    paris = ZoneInfo("Europe/Paris")
    quarters = day(on, base=40, zone="Europe/Paris")
    hourly = Period(quarters.start, quarters.end, [77.0] * 24, resolution="PT60M")
    fake = FakeEntsoE([hourly, quarters], eic="10YFR-RTE------C")
    async with plugin(fake, await secrets(tmp_path), clock, zone="FR") as (_, link, _):
        await link.describe(timeout=5)
        got = await spot(link, local(2026, 10, 7, 0, paris), days=1)
    assert "classificationSequence_AttributeInstanceComponent.position" not in fake.asked[0]
    assert len(got) == 96
    assert {i.end - i.start for i in got} == {timedelta(minutes=15)}
    assert got[1].value == pytest.approx(0.0405)


async def test_nothing_published_is_stale_then_fresh(tmp_path: Path) -> None:
    fake = FakeEntsoE([day(date(2026, 10, 7))])
    clock = Clock(datetime(2026, 10, 7, 15, 30, tzinfo=ZoneInfo("Europe/Brussels")))
    async with await Database.open(tmp_path / "t.db") as db:
        series = Series(db, clock)
        async with plugin(fake, await secrets(tmp_path), clock, currency="EUR") as (
            _,
            link,
            health,
        ):
            described = await link.describe(timeout=5)
            following = asyncio.create_task(series.follow("entsoe", link, described.series))
            key = Key("entsoe", "spot")
            async with asyncio.timeout(5):
                while not health or series.followed.get(key) is None:
                    await asyncio.sleep(0.01)
                while series.followed[key].known_until is None:
                    await asyncio.sleep(0.01)
            assert series.freshness(key)[0] == "stale"
            assert health[-1].stale == ("spot",)
            fake.periods.append(day(date(2026, 10, 8)))
            async with asyncio.timeout(5):
                while series.freshness(key)[0] != "fresh":
                    await asyncio.sleep(0.01)
            following.cancel()
            await asyncio.gather(following, return_exceptions=True)


async def test_no_prices_at_all_is_an_answer(tmp_path: Path) -> None:
    fake = FakeEntsoE([])
    async with plugin(fake, await secrets(tmp_path), Clock(local(2026, 10, 7, 10))) as (
        _,
        link,
        health,
    ):
        described = await link.describe(timeout=5)
        assert [s.id for s in described.series] == ["spot"]
        assert await spot(link, local(2026, 10, 7)) == []
        async with asyncio.timeout(5):
            while not health:
                await asyncio.sleep(0.01)
    assert health[-1].state == "up"


async def test_a_refused_token_asks_the_user_and_stops_asking(tmp_path: Path) -> None:
    fake = FakeEntsoE([day(date(2026, 10, 7))])
    store = await secrets(tmp_path, token="not-a-token-0000")
    async with plugin(fake, store, Clock(local(2026, 10, 7, 10))) as (_, link, health):
        await link.describe(timeout=5)
        async with asyncio.timeout(5):
            while not health:
                await asyncio.sleep(0.01)
        await asyncio.sleep(0.3)
    assert "refused the security token" in (health[-1].needs_user_action or "")
    assert len(fake.asked) == 1
    assert all("not-a-token-0000" not in str(h.model_dump()) for h in health)


async def test_it_conforms(tmp_path: Path) -> None:
    now = datetime.now(STOCKHOLM).date()
    fake = FakeEntsoE([day(now), day(now + timedelta(days=1))], rates=rates())
    async with running(fake) as (url, ecb):
        p = EntsoEPlugin(
            EntsoE(token="entsoe.token", zone="SE3"),
            secrets=await secrets(tmp_path),
            url=url,
            rates=Rates(ecb),
        )
        core, plugin_side = pair()
        served = asyncio.create_task(serve(plugin_side, p))
        try:
            assert list(await run(core, timeout_s=20, quiet_s=0.3)) == []
        finally:
            await plugin_side.close()
            served.cancel()
            await asyncio.gather(served, return_exceptions=True)
