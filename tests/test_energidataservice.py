"""Denmark's grid tariffs from Energi Data Service: a household's grid company tariff with
its rebate, Energinet's tariffs and the electricity tax, hour by hour; the list of grid
companies to pick from. Against a fake of DataHub's price list, from real rows."""

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import aiohttp
import pytest
from aiohttp import web
from test_spot_sources import Clock, fast, got, linked

from thermaestro.cap import pair, serve
from thermaestro.cap.conformance import run
from thermaestro.energidataservice.plugin import EnergiDataServicePlugin, companies
from thermaestro.store.settings import DanishGrid

COPENHAGEN = ZoneInfo("Europe/Copenhagen")
RADIUS = "5790000705689"
ENERGINET = "5790000432752"
NIGHT, DAY, PEAK = 0.106175, 0.318524, 0.955573


def row(
    gln: str, code: str, note: str, start: str, end: str | None, **prices: Any
) -> dict[str, Any]:
    out: dict[str, Any] = {
        "ChargeOwner": None if gln == ENERGINET else "Radius Elnet A/S",
        "GLN_Number": gln,
        "ChargeType": "D03",
        "ChargeTypeCode": code,
        "Note": note,
        "ValidFrom": f"{start}T00:00:00",
        "ValidTo": f"{end}T00:00:00" if end else None,
        "VATClass": "D02",
        "ResolutionDuration": "PT1H" if len(prices) > 1 else "P1D",
    }
    for n in range(1, 25):
        out[f"Price{n}"] = prices.get(f"p{n}")
    return out


def hourly(by_hour: list[float]) -> dict[str, float]:
    return {f"p{n + 1}": v for n, v in enumerate(by_hour)}


RADIUS_C = hourly([NIGHT] * 6 + [DAY] * 11 + [PEAK] * 4 + [DAY] * 3)  # Radius, Oct 2026
ROWS = [
    row(RADIUS, "DT_C_01", "Nettarif C", "2026-10-01", "2027-04-01", **RADIUS_C),
    row(RADIUS, "DT_C_01", "Nettarif C", "2026-04-01", "2026-10-01", **hourly([0.2] * 24)),
    row(ENERGINET, "40000", "Transmissions nettarif", "2026-01-01", "2027-01-01", p1=0.043),
    row(ENERGINET, "41000", "Systemtarif", "2026-01-01", "2027-01-01", p1=0.072),
    row(ENERGINET, "EA-001", "Elafgift", "2026-01-01", "2028-01-01", p1=0.008),
]


class Fake:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.asked: list[dict[str, Any]] = []

    async def pricelist(self, request: web.Request) -> web.Response:
        where = json.loads(request.query.get("filter", "{}"))
        self.asked.append(where)
        found = [r for r in self.rows if all(r.get(k) in v for k, v in where.items())]
        body = {"total": len(found), "dataset": "DatahubPricelist", "records": found}
        return web.json_response(body)


@contextlib.asynccontextmanager
async def running(fake: Fake) -> AsyncIterator[str]:
    app = web.Application()
    app.router.add_get("/dataset/DatahubPricelist", fake.pricelist)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
    try:
        yield f"http://127.0.0.1:{port}/dataset/DatahubPricelist"
    finally:
        await runner.cleanup()


SETTINGS = DanishGrid(gln=RADIUS, codes=("DT_C_01",), company="Radius Elnet A/S")


async def test_a_households_tariffs_hour_by_hour() -> None:
    clock = Clock(datetime(2026, 10, 20, 10, tzinfo=COPENHAGEN))
    async with running(Fake(ROWS)) as url:
        p = EnergiDataServicePlugin(SETTINGS, url=url, **fast(clock))
        async with linked(p) as (link, _):
            described = await link.describe(timeout=5)
            roles = {s.id: (s.role, s.unit, s.vat) for s in described.series}
            assert roles == {
                "grid": ("grid.tou", "DKK/kWh", "excl"),
                "energinet": ("grid.transfer", "DKK/kWh", "excl"),
                "elafgift": ("tax.energy", "DKK/kWh", "excl"),
            }
            day = datetime(2026, 10, 20, tzinfo=COPENHAGEN)
            grid = await got(link, "grid", day, days=1)
            energinet = await got(link, "energinet", day, days=1)
            tax = await got(link, "elafgift", day, days=1)
    by_hour = {i.start.astimezone(COPENHAGEN).hour: i.value for i in grid}
    assert len(grid) == 24
    assert (by_hour[5], by_hour[6], by_hour[17], by_hour[21]) == (NIGHT, DAY, PEAK, DAY)
    assert {i.value for i in energinet} == {0.115}  # transmission + system
    assert energinet[0].source == "calculated"
    assert energinet[0].why == "40000 + 41000"
    assert {i.value for i in tax} == {0.008}


async def test_the_day_the_clock_goes_back_and_a_rebate() -> None:
    rebate = row(RADIUS, "DT_C_R", "Rabat Nettarif C", "2026-10-01", "2027-01-01",
                 **hourly([0.0] * 17 + [-0.1] * 4 + [0.0] * 3))  # fmt: skip
    settings = SETTINGS.model_copy(update={"codes": ("DT_C_01", "DT_C_R")})
    clock = Clock(datetime(2026, 10, 25, 10, tzinfo=COPENHAGEN))
    async with running(Fake([*ROWS, rebate])) as url:
        p = EnergiDataServicePlugin(settings, url=url, **fast(clock))
        async with linked(p) as (link, _):
            await link.describe(timeout=5)
            grid = await got(link, "grid", datetime(2026, 10, 25, tzinfo=COPENHAGEN), days=1)
    assert len(grid) == 25  # 02:00 comes twice, at the same price
    twice = [i.value for i in grid if i.start.astimezone(COPENHAGEN).hour == 2]
    assert twice == [NIGHT, NIGHT]
    by_hour = {i.start.astimezone(COPENHAGEN).hour: i.value for i in grid}
    assert by_hour[18] == pytest.approx(PEAK - 0.1)


async def test_the_grid_companies_to_pick_from() -> None:
    other = "5790000705184"
    found = [
        *ROWS,
        {**row(other, "30TR_C_ET", "Nettarif C", "2026-01-01", "2027-01-01", p1=0.2),
         "ChargeOwner": "Cerius A/S"},
        {**row(other, "30RE_C_ET", "Rabat Nettarif C", "2026-01-01", "2027-01-01", p1=0.0),
         "ChargeOwner": "Cerius A/S"},
        {**row(other, "OLD", "Nettarif C", "2024-01-01", "2025-01-01", p1=0.2),
         "ChargeOwner": "Cerius A/S"},
    ]  # fmt: skip
    async with running(Fake(found)) as url, aiohttp.ClientSession() as session:
        listed = await companies(session, date(2026, 10, 20), url)
    assert [(c["company"], c["gln"], c["codes"]) for c in listed] == [
        ("Cerius A/S", other, ["30TR_C_ET", "30RE_C_ET"]),
        ("Radius Elnet A/S", RADIUS, ["DT_C_01"]),
    ]


async def test_it_conforms() -> None:
    today = datetime.now(COPENHAGEN).date()
    current = [
        {**r, "ValidFrom": f"{today - timedelta(days=30)}T00:00:00", "ValidTo": None}
        for r in ROWS[:1] + ROWS[2:]
    ]
    async with running(Fake(current)) as url:
        p = EnergiDataServicePlugin(SETTINGS, url=url)
        core, plugin_side = pair()
        served = asyncio.create_task(serve(plugin_side, p))
        try:
            assert list(await run(core, timeout_s=20, quiet_s=0.3)) == []
        finally:
            await plugin_side.close()
            served.cancel()
            await asyncio.gather(served, return_exceptions=True)
