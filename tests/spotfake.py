"""Small stand-ins for the price sources that need no account: Energy-Charts' `/price`,
Beneficial Apps' Nordic price sites' daily files, OMIE's marginalpdbc files and Octopus'
products and unit rates, plus the ECB's 90-day reference rates. The formats are the real
ones as read on 2026-10-08; the values are invented."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from aiohttp import web
from aiohttp.test_utils import TestServer

PRIVATE = (
    "The data provided herein is for private and internal use only. The utilization of any"
    " data, whether in its raw or derived form, for external or commercial purposes is"
    " expressly prohibited. Should you require licensing for market-related data, please"
    " direct your inquiries to the original data providers, including but not limited to"
    " EPEX SPOT SE."
)
CC_BY = "CC BY 4.0 (creativecommons.org/licenses/by/4.0) from Bundesnetzagentur | SMARD.de"


def local_day(on: date, zone: str, step: timedelta) -> list[tuple[datetime, datetime]]:
    """A local day's intervals, from midnight to midnight."""
    tz = ZoneInfo(zone)
    t = datetime.combine(on, time(0), tz).astimezone(UTC)
    end = datetime.combine(on + timedelta(days=1), time(0), tz).astimezone(UTC)
    out = []
    while t < end:
        out.append((t, t + step))
        t += step
    return out


def price(n: int, base: float) -> float:
    return round(base + n * 0.5, 2)


@dataclass
class Fake:
    ec: dict[tuple[str, date], tuple[str, timedelta, float]] = field(default_factory=dict)
    """Energy-Charts: (its zone name, day) to (time zone, step, base price)."""
    ec_license: dict[str, str] = field(default_factory=dict)
    nordic: dict[str, tuple[str, timedelta, float]] = field(default_factory=dict)
    """A Nordic file's path (`2026/10-09_SE3`) to (time zone, step, base EUR/MWh)."""
    omie: dict[date, tuple[int, float]] = field(default_factory=dict)
    """A day to (periods, base price); Portugal's are 1 more than Spain's."""
    octopus: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    """A tariff code to its unit rates."""
    products: list[dict[str, Any]] = field(default_factory=list)
    rates: dict[date, dict[str, float]] = field(default_factory=dict)
    asked: list[str] = field(default_factory=list)

    async def energy_charts(self, request: web.Request) -> web.Response:
        q = request.query
        self.asked.append(f"ec {q['bzn']} {q['start']} {q['end']}")
        start, end = date.fromisoformat(q["start"]), date.fromisoformat(q["end"])
        seconds: list[int] = []
        prices: list[float] = []
        on = start
        while on <= end:
            found = self.ec.get((q["bzn"], on))
            if found:
                zone, step, base = found
                for n, (t, _) in enumerate(local_day(on, zone, step)):
                    seconds.append(int(t.timestamp()))
                    prices.append(price(n, base))
            on += timedelta(days=1)
        if not seconds:
            return web.json_response({"detail": "no content available"}, status=404)
        return web.json_response(
            {
                "license_info": self.ec_license.get(q["bzn"], PRIVATE),
                "unix_seconds": seconds,
                "price": prices,
                "unit": "EUR / MWh",
                "deprecated": False,
            }
        )

    async def nordic_file(self, request: web.Request) -> web.Response:
        path = f"{request.match_info['year']}/{request.match_info['name']}"
        self.asked.append(f"nordic {path}")
        found = self.nordic.get(path.removesuffix(".json"))
        if found is None:
            return web.Response(status=404, text="<p>404: Data not found or not ready yet</p>")
        zone, step, base = found
        year, name = path.removesuffix(".json").split("/")
        on = date(int(year), int(name[:2]), int(name[3:5]))
        tz = ZoneInfo(zone)
        rows = [
            {
                "EUR_per_kWh": round(price(n, base) / 1000, 5),
                "time_start": s.astimezone(tz).isoformat(),
                "time_end": e.astimezone(tz).isoformat(),
            }
            for n, (s, e) in enumerate(local_day(on, zone, step))
        ]
        return web.json_response(rows)

    async def omie_file(self, request: web.Request) -> web.Response:
        name = request.query.get("filename", "")
        self.asked.append(f"omie {name}")
        try:
            on = datetime.strptime(name, "marginalpdbc_%Y%m%d.1").date()
        except ValueError:
            return web.Response(status=404, text="<!DOCTYPE html>", content_type="text/html")
        if on not in self.omie:
            return web.Response(status=404, text="<!DOCTYPE html>", content_type="text/html")
        periods, base = self.omie[on]
        lines = ["MARGINALPDBC;"]
        for n in range(1, periods + 1):
            es = price(n, base)
            lines.append(f"{on.year};{on.month:02};{on.day:02};{n};{es + 1};{es};")
        lines.append("*")
        return web.Response(
            body="\r\n".join(lines).encode(), content_type="application/octet-stream"
        )

    async def octopus_products(self, request: web.Request) -> web.Response:
        self.asked.append("octopus products")
        return web.json_response(
            {"count": len(self.products), "next": None, "previous": None, "results": self.products}
        )

    async def octopus_rates(self, request: web.Request) -> web.Response:
        tariff = request.match_info["tariff"]
        self.asked.append(f"octopus {tariff}")
        rates = self.octopus.get(tariff)
        if rates is None:
            return web.json_response({"detail": "Not found."}, status=404)
        return web.json_response({"count": len(rates), "next": None, "results": rates})

    async def ecb(self, request: web.Request) -> web.Response:
        days = "".join(
            f'<Cube time="{d.isoformat()}">'
            + "".join(f'<Cube currency="{c}" rate="{r}"/>' for c, r in rates.items())
            + "</Cube>"
            for d, rates in sorted(self.rates.items(), reverse=True)
        )
        body = (
            '<?xml version="1.0" encoding="UTF-8"?><gesmes:Envelope'
            ' xmlns:gesmes="http://www.gesmes.org/xml/2002-08-01"'
            ' xmlns="http://www.ecb.int/vocabulary/2002-08-01/eurofxref">'
            f"<Cube>{days}</Cube></gesmes:Envelope>"
        )
        return web.Response(text=body, content_type="text/xml")


def agile_rates(on: date, base_pence: float) -> list[dict[str, Any]]:
    """Agile's half hours from 23:00 the evening before to 23:00, newest first, as the API
    lists them. VAT is 0 % on domestic electricity from 2026-10-01."""
    london = ZoneInfo("Europe/London")
    t = datetime.combine(on - timedelta(days=1), time(23), london).astimezone(UTC)
    end = datetime.combine(on, time(23), london).astimezone(UTC)
    out = []
    n = 0
    while t < end:
        value = round(base_pence + n * 0.1, 2)
        out.append(
            {
                "value_exc_vat": value,
                "value_inc_vat": value,
                "valid_from": t.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "valid_to": (t + timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "payment_method": None,
            }
        )
        t += timedelta(minutes=30)
        n += 1
    return list(reversed(out))


@dataclass
class Urls:
    energy_charts: str
    nordic: str
    omie: str
    octopus: str
    ecb: str


@asynccontextmanager
async def running(fake: Fake) -> AsyncIterator[Urls]:
    app = web.Application()
    app.router.add_get("/price", fake.energy_charts)
    app.router.add_get("/nordic/api/v1/prices/{year}/{name}", fake.nordic_file)
    app.router.add_get("/es/file-download", fake.omie_file)
    app.router.add_get("/v1/products/", fake.octopus_products)
    app.router.add_get(
        "/v1/products/{product}/electricity-tariffs/{tariff}/standard-unit-rates/",
        fake.octopus_rates,
    )
    app.router.add_get("/eurofxref-hist-90d.xml", fake.ecb)
    server = TestServer(app, host="127.0.0.1")
    await server.start_server()
    base = f"http://127.0.0.1:{server.port}"
    try:
        yield Urls(
            f"{base}/price",
            f"{base}/nordic",
            f"{base}/es/file-download",
            f"{base}/v1",
            f"{base}/eurofxref-hist-90d.xml",
        )
    finally:
        await server.close()
