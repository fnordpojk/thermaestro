"""The prices page and API: layers, VAT, the stack per day, and the series offered."""

import html
import json
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import pytest
from test_core_series import OTHER_SPOT, SPOT, TOTAL, quarters
from test_web import ADMIN_PASSWORD, FAST, KEY, ORIGIN, Clock, Site, csrf_of, form

from thermaestro.auth import Accounts, AddressLimiter, SetupCode
from thermaestro.core import AuditLog
from thermaestro.core.host import PluginHost
from thermaestro.core.prices import slots
from thermaestro.core.series import Series
from thermaestro.core.values import Values
from thermaestro.store import Database, Location, SecretStore
from thermaestro.web import Services, create_app

STOCKHOLM = ZoneInfo("Europe/Stockholm")


@pytest.fixture
async def site(tmp_path: Path) -> AsyncIterator[Site]:
    clock = Clock()
    async with await Database.open(tmp_path / "t.db") as db:
        audit = AuditLog(tmp_path / "audit")
        values = Values(db)
        secrets = SecretStore(tmp_path / "secrets.json")
        series = Series(db)
        series.describe("tibber", [SPOT, TOTAL])
        today = datetime.now(STOCKHOLM).replace(hour=0, minute=0, second=0, microsecond=0)
        n = len(slots(today.date(), STOCKHOLM))  # 92, 96 or 100
        await series.put("tibber", quarters(SPOT.id, today, n, 0.59016))
        await db.put(Location(latitude=59.33, longitude=18.07, timezone="Europe/Stockholm"))
        services = Services(
            accounts=Accounts(
                db, audit, hasher=FAST, clock=clock, limiter=AddressLimiter(tries=1000)
            ),
            db=db,
            values=values,
            host=PluginHost(
                db=db, secrets=secrets, values=values, audit=audit, factories={}, series=series
            ),
            audit=audit,
            secrets=secrets,
            setup=SetupCode(tmp_path / "setup-code", clock),
            series=series,
        )
        await services.load_zone()
        yield Site(create_app(services, KEY), services, clock, tmp_path)


@asynccontextmanager
async def logged_in(site: Site) -> AsyncIterator[httpx.AsyncClient]:
    await site.services.accounts.create_user("admin", ADMIN_PASSWORD, ["Administrators"], by="cli")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=site.app), base_url=ORIGIN, headers={"origin": ORIGIN}
    ) as client:
        page = await client.get("/login")
        await client.post(
            "/login", data={"csrf": csrf_of(page.text), "name": "admin", "password": ADMIN_PASSWORD}
        )
        yield client


async def test_building_the_stack_on_the_page(site: Site) -> None:
    async with logged_in(site) as client:
        page = (await client.get("/prices")).text
        assert "tibber · se3/spot" in page  # offered
        await form(
            client, "/setup/prices", "/prices/layers", source="series", offered="tibber:se3/spot"
        )
        await form(
            client,
            "/setup/prices",
            "/prices/layers",
            source="fixed",
            role="energy.supplier",
            value="0,0998",
            unit="SEK/kWh",
            vat="excl",
        )
        await form(
            client,
            "/setup/prices",
            "/prices/layers",
            source="fixed",
            role="tax.energy",
            value="0.360",
            unit="SEK/kWh",
            vat="excl",
        )
        await form(
            client,
            "/setup/prices",
            "/prices/layers",
            source="fixed",
            role="grid.transfer",
            value="0.3116",
            unit="SEK/kWh",
            vat="excl",
        )
        layers = (await client.get("/api/v1/prices/layers")).json()
        assert sorted(layers) == ["energy-spot", "energy-supplier", "grid-transfer", "tax-energy"]
        saved = await client.post(
            "/prices/vat",
            data={
                "csrf": csrf_of((await client.get("/setup/prices")).text),
                "rate": "25",
                "applies_to": list(layers),
            },
        )
        assert saved.status_code == 303
        today = (await client.get("/api/v1/prices")).json()
        assert today["problems"] == []
        assert today["unit"] == "SEK/kWh"
        assert len(today["slots"]) in (92, 96, 100)
        assert today["slots"][0]["total"] == pytest.approx(1.702, abs=0.001)
        page = (await client.get("/prices")).text
        assert "1.702" in page
        tomorrow = (datetime.now(STOCKHOLM) + timedelta(days=1)).date().isoformat()
        empty = (await client.get("/api/v1/prices", params={"day": tomorrow})).json()
        assert all(slot["total"] is None for slot in empty["slots"])


async def test_the_price_chart_and_its_table(site: Site) -> None:
    """The chart takes its days from the API and names each layer by its role; the table
    stays in the page for when the chart is switched off, or there's no script."""
    async with logged_in(site) as client:
        await form(
            client, "/setup/prices", "/prices/layers", source="series", offered="tibber:se3/spot"
        )
        await form(
            client,
            "/setup/prices",
            "/prices/layers",
            source="fixed",
            role="tax.energy",
            value="0.360",
            unit="SEK/kWh",
            vat="excl",
        )
        page = (await client.get("/prices")).text
    chart = re.search(r"data-chart='([^']+)'", page)
    assert chart
    config = json.loads(html.unescape(chart.group(1)))
    assert config["names"] == {
        "energy-spot": "Spot price \N{MIDDLE DOT} tibber",
        "tax-energy": "Energy tax",
        "vat": "VAT",
    }
    today = datetime.now(STOCKHOLM).date()
    assert config["days"] == [today.isoformat(), (today + timedelta(days=1)).isoformat()]
    assert 'data-source="/api/v1/prices"' in page
    assert 'data-view-of="prices" data-show="table"' in page
    assert "Per 15 minutes" in page


async def test_a_double_count_is_shown_not_summed(site: Site) -> None:
    async with logged_in(site) as client:
        await form(
            client, "/setup/prices", "/prices/layers", source="series", offered="tibber:se3/spot"
        )
        await form(
            client,
            "/setup/prices",
            "/prices/layers",
            source="series",
            offered="tibber:home/price.total",
        )
        today = (await client.get("/api/v1/prices")).json()
        assert today["problems"] == [
            "energy.spot is counted twice: in energy-spot and in energy-supplier"
        ]
        assert today["slots"] == []
        assert "energy.spot is counted twice" in (await client.get("/prices")).text


async def test_series_and_their_freshness(site: Site) -> None:
    async with logged_in(site) as client:
        offered = (await client.get("/api/v1/series")).json()
    spot = next(o for o in offered if o["series"] == "se3/spot")
    assert (spot["role"], spot["unit"]) == ("energy.spot", "SEK/kWh")
    assert spot["freshness"] in ("fresh", "stale")
    total = next(o for o in offered if o["series"] == "home/price.total")
    assert total["freshness"] == "empty"


async def test_price_sources_in_the_settings(site: Site) -> None:
    async with logged_in(site) as client:
        page = (await client.get("/setup/prices")).text
        assert "SE3 \N{EN DASH} Sweden" in page
        await form(client, "/setup/prices", "/settings/tibber", token="tibber-token-1")
        await form(client, "/setup/prices", "/settings/entsoe", zone="SE3", token="entsoe-token-1")
        sources = (await client.get("/api/v1/prices/sources")).json()
        assert sources["tibber"]["settings"] == {"token": "tibber.token", "home": None}
        assert sources["entsoe"]["settings"] == {
            "token": "entsoe.token",
            "zone": "SE3",
            "currency": "SEK",  # the zone's own
        }
        secret = await site.services.secrets.get("entsoe.token")
        assert secret is not None
        assert secret.get_secret_value() == "entsoe-token-1"
        # Saved again without a token: the one entered is kept.
        await form(client, "/setup/prices", "/settings/entsoe", zone="SE4", currency="eur")
        sources = (await client.get("/api/v1/prices/sources")).json()
        assert (
            sources["entsoe"]["settings"]["zone"],
            sources["entsoe"]["settings"]["currency"],
        ) == (
            "SE4",
            "EUR",
        )
        refused = await client.post(
            "/settings/entsoe",
            data={"csrf": csrf_of((await client.get("/setup/prices")).text), "zone": "XX9"},
        )
        assert refused.status_code == 400
        assert "no bidding zone" in refused.text
        assert "tibber-token-1" not in (await client.get("/setup/prices")).text


async def test_a_fallback_and_the_check_against_tibber(site: Site) -> None:
    series = site.services.series
    assert series is not None
    series.describe("entsoe", [OTHER_SPOT])
    today = datetime.now(STOCKHOLM).replace(hour=0, minute=0, second=0, microsecond=0)
    tomorrow = (today + timedelta(days=1)).replace(tzinfo=None).replace(tzinfo=STOCKHOLM)
    n = len(slots(tomorrow.date(), STOCKHOLM))
    await series.put("entsoe", quarters("spot", tomorrow, n, 0.61))
    n_today = len(slots(today.date(), STOCKHOLM))
    await series.put(
        "tibber", quarters(TOTAL.id, today, n_today, round(1.25 * 0.59016 + 0.1248, 6), vat="incl")
    )
    async with logged_in(site) as client:
        await form(
            client, "/setup/prices", "/prices/layers", source="series", offered="tibber:se3/spot"
        )
        await form(
            client,
            "/setup/prices",
            "/prices/layers",
            source="fixed",
            role="energy.supplier",
            value="0.0998",
            unit="SEK/kWh",
            vat="excl",
        )
        await client.post(
            "/prices/vat",
            data={
                "csrf": csrf_of((await client.get("/setup/prices")).text),
                "rate": "25",
                "applies_to": ["energy-spot", "energy-supplier"],
            },
        )
        page = (await client.get("/setup/prices")).text
        assert 'action="/prices/layers/energy-spot/fallbacks"' in page
        await form(
            client, "/setup/prices", "/prices/layers/energy-spot/fallbacks", fallbacks="entsoe:spot"
        )
        layers = (await client.get("/api/v1/prices/layers")).json()
        assert layers["energy-spot"]["fallbacks"] == ["entsoe:spot"]
        today_stack = (await client.get("/api/v1/prices")).json()
        [checked] = today_stack["checks"]
        assert (checked["series"], checked["differing"]) == ("tibber:home/price.total", 0)
        assert checked["compared"] == n_today
        page = (await client.get("/prices")).text
        assert "Checked against tibber:home/price.total" in page
        later = (
            await client.get("/api/v1/prices", params={"day": tomorrow.date().isoformat()})
        ).json()
        spot = later["slots"][0]["parts"][0]
        assert (spot["layer"], spot["fallback"]) == ("energy-spot", "entsoe:spot")
        assert spot["value"] == pytest.approx(1.25 * 0.61)
