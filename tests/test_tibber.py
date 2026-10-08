"""The Tibber plugin against a stand-in for Tibber's API."""

import asyncio
import contextlib
from collections.abc import AsyncIterator
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from tibberfake import STOCKHOLM, TOKEN, FakeTibber, day, running

from thermaestro.cap import Link, pair, serve
from thermaestro.cap.conformance import run
from thermaestro.cap.messages import Health
from thermaestro.core import discover
from thermaestro.core.series import Key, Series
from thermaestro.store import Database, SecretStore, Tibber
from thermaestro.tibber.plugin import TibberPlugin


class Clock:
    def __init__(self, at: datetime) -> None:
        self.now = at.timestamp()

    def __call__(self) -> float:
        return self.now


def local(y: int, m: int, d: int, h: int = 0) -> datetime:
    return datetime(y, m, d, h, tzinfo=STOCKHOLM)


async def secrets(tmp_path: Path, token: str = TOKEN) -> SecretStore:
    store = SecretStore(tmp_path / "secrets.json")
    await store.set("tibber.token", token)
    return store


@contextlib.asynccontextmanager
async def plugin(
    url: str, store: SecretStore, clock: Clock, **kw: Any
) -> AsyncIterator[tuple[TibberPlugin, Link, list[Health]]]:
    p = TibberPlugin(
        Tibber(token="tibber.token", **kw),
        secrets=store,
        url=url,
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


async def test_the_series_and_what_they_cover(tmp_path: Path) -> None:
    fake = FakeTibber(date(2026, 10, 7))
    async with (
        running(fake) as url,
        plugin(url, await secrets(tmp_path), Clock(local(2026, 10, 7, 10))) as (_, link, _),
    ):
        described = await link.describe(timeout=5)
        energy, total = described.series
        assert (energy.id, energy.role, energy.unit, energy.vat) == (
            "energy",
            "energy.spot",
            "SEK/kWh",
            "excl",
        )
        assert (total.id, total.role, total.vat) == ("total", "energy.supplier", "incl")
        assert energy.area == total.area == "SE3"  # the home's price area, as Tibber gives it
        assert "meteringPointData { priceAreaCode }" in fake.queries[0]
        assert "consumptionEan" not in fake.queries[0]
        assert total.covers.value == ("energy.spot", "vat")
        assert total.covers.known == "verified"  # checked on a Swedish home
        assert energy.publication is not None
        assert (energy.publication.daily_after, energy.publication.tz) == (
            "13:00",
            "Europe/Stockholm",
        )
        assert described.provider is not None
        assert described.provider.name == "Tibber"
        start = local(2026, 10, 7)
        data = await link.series_get("total", start, start + timedelta(days=2), timeout=5)
    assert len(data.intervals) == 96
    first = data.intervals[0]
    assert (first.start, first.end) == (start, start + timedelta(minutes=15))
    assert first.value == pytest.approx(1.25 * 0.5 + 0.1248)
    assert data.intervals[-1].end == local(2026, 10, 8)


async def test_only_price_fields_are_asked_for(tmp_path: Path) -> None:
    fake = FakeTibber(date(2026, 10, 7))
    async with (
        running(fake) as url,
        plugin(url, await secrets(tmp_path), Clock(local(2026, 10, 7, 10))) as (_, link, _),
    ):
        await link.describe(timeout=5)
    asked = fake.queries[0]
    for personal in (
        "address",
        "subscriber",
        "contactInfo",
        "name",
        "consumption",
        "productionEan",
        "gridCompany",
        "gridAreaCode",
        "energyTaxType",
        "estimatedAnnual",
    ):
        assert personal not in asked
    # Of the metering point, only its price area (SE3), which every home in it shares.
    assert "meteringPointData { priceAreaCode }" in asked
    assert "QUARTER_HOURLY" in asked


@pytest.mark.parametrize(("on", "n"), [(date(2026, 3, 29), 92), (date(2026, 10, 25), 100)])
async def test_days_that_change_the_clock(tmp_path: Path, on: date, n: int) -> None:
    fake = FakeTibber(on)
    clock = Clock(datetime(on.year, on.month, on.day, 10, tzinfo=STOCKHOLM))
    async with running(fake) as url, plugin(url, await secrets(tmp_path), clock) as (_, link, _):
        await link.describe(timeout=5)
        start = datetime(on.year, on.month, on.day, tzinfo=STOCKHOLM)
        data = await link.series_get("energy", start, start + timedelta(days=2), timeout=5)
    assert len(data.intervals) == n
    for a, b in zip(data.intervals, data.intervals[1:], strict=False):
        assert a.end == b.start
        assert b.end - b.start == timedelta(minutes=15)
    assert data.intervals[-1].end == datetime(on.year, on.month, on.day + 1, tzinfo=STOCKHOLM)


async def test_tomorrow_late_is_stale_until_it_comes(tmp_path: Path) -> None:
    fake = FakeTibber(date(2026, 10, 7))
    clock = Clock(local(2026, 10, 7, 15) + timedelta(minutes=30))  # due at 13:00
    async with await Database.open(tmp_path / "t.db") as db:
        series = Series(db, clock)
        async with (
            running(fake) as url,
            plugin(url, await secrets(tmp_path), clock) as (_, link, health),
        ):
            described = await link.describe(timeout=5)
            following = asyncio.create_task(series.follow("tibber", link, described.series))
            key = Key("tibber", "energy")
            async with asyncio.timeout(5):
                while series.followed.get(key) is None or series.followed[key].known_until is None:
                    await asyncio.sleep(0.01)
                while not health:
                    await asyncio.sleep(0.01)
            assert series.freshness(key) == (
                "stale",
                "nothing for tomorrow yet, though due at 13:00",
            )
            assert health[-1].stale == ("energy", "total")
            fake.tomorrow = day(date(2026, 10, 8), base=0.7)
            async with asyncio.timeout(5):
                while series.freshness(key)[0] != "fresh":
                    await asyncio.sleep(0.01)
            tomorrow = await series.get(
                key, local(2026, 10, 8).timestamp(), local(2026, 10, 9).timestamp()
            )
            assert len(tomorrow) == 96
            assert tomorrow[0].value == 0.7
            async with asyncio.timeout(5):
                while health[-1].stale:
                    await asyncio.sleep(0.01)
            following.cancel()
            await asyncio.gather(following, return_exceptions=True)


async def test_a_refused_token_asks_the_user_and_stops_asking(tmp_path: Path) -> None:
    fake = FakeTibber(date(2026, 10, 7))
    store = await secrets(tmp_path, token="expired")
    async with (
        running(fake) as url,
        plugin(url, store, Clock(local(2026, 10, 7, 10))) as (_, link, health),
    ):
        described = await link.describe(timeout=5)
        assert described.series == ()
        async with asyncio.timeout(5):
            while not health:
                await asyncio.sleep(0.01)
        await asyncio.sleep(0.3)  # several polls' worth
    assert health[-1].state == "down"
    assert "refused the token" in (health[-1].needs_user_action or "")
    assert len(fake.queries) == 1


async def test_a_chosen_home_that_isnt_there(tmp_path: Path) -> None:
    fake = FakeTibber(date(2026, 10, 7))
    async with (
        running(fake) as url,
        plugin(url, await secrets(tmp_path), Clock(local(2026, 10, 7, 10)), home="another") as (
            _,
            link,
            health,
        ),
    ):
        await link.describe(timeout=5)
        async with asyncio.timeout(5):
            while not health:
                await asyncio.sleep(0.01)
    assert health[-1].needs_user_action == "the chosen home isn't on this Tibber account"


async def test_it_conforms(tmp_path: Path) -> None:
    fake = FakeTibber(date(2026, 10, 7))
    async with running(fake) as url:
        p = TibberPlugin(Tibber(token="tibber.token"), secrets=await secrets(tmp_path), url=url)
        core, plugin_side = pair()
        served = asyncio.create_task(serve(plugin_side, p))
        try:
            assert list(await run(core, timeout_s=20, quiet_s=0.3)) == []
        finally:
            await plugin_side.close()
            served.cancel()
            await asyncio.gather(served, return_exceptions=True)


def test_it_is_installed() -> None:
    assert "tibber" in discover()
    assert "entsoe" in discover()
