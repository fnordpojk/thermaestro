"""Small stand-ins for the ENTSO-E Transparency Platform's API (day-ahead prices, A44) and
the ECB's 90-day reference rates, in the formats their documentation shows (the values
invented)."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from aiohttp import web
from aiohttp.test_utils import TestServer

TOKEN = "good-token"
SE3 = "10Y1001A1001A46L"
DOCUMENT = "urn:iec62325.351:tc57wg16:451-3:publicationdocument:7:3"
ACK = "urn:iec62325.351:tc57wg16:451-1:acknowledgementdocument:7:0"


@dataclass
class Period:
    start: datetime
    end: datetime
    prices: list[float]
    """EUR/MWh, one per step."""
    resolution: str = "PT15M"
    sequence: int | None = None


def day(
    on: date, base: float = 50.0, zone: str = "Europe/Brussels", sequence: int | None = None
) -> Period:
    """A market day's quarters: the auction's day is Central European in every zone, as the
    real API's answers show. Every fourth price repeats the one before, as prices often do,
    so the A03 curve leaves it out."""
    tz = ZoneInfo(zone)
    start = datetime.combine(on, time(0), tz).astimezone(UTC)
    end = datetime.combine(on + timedelta(days=1), time(0), tz).astimezone(UTC)
    n = int((end - start) / timedelta(minutes=15))
    prices: list[float] = []
    for i in range(n):
        prices.append(prices[-1] if i % 4 == 3 else round(base + i * 0.5, 2))
    return Period(start, end, prices, sequence=sequence)


def _t(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%MZ")


def document(eic: str, periods: list[Period]) -> str:
    series = []
    for n, p in enumerate(periods, 1):
        points = []
        for i, price in enumerate(p.prices):
            if i and price == p.prices[i - 1]:
                continue  # A03: only where the price changes
            points.append(
                f"<Point><position>{i + 1}</position><price.amount>{price}</price.amount></Point>"
            )
        sequence = (
            f"<classificationSequence_AttributeInstanceComponent.position>{p.sequence}"
            "</classificationSequence_AttributeInstanceComponent.position>"
            if p.sequence is not None
            else ""
        )
        series.append(
            f"<TimeSeries><mRID>{n}</mRID><auction.type>A01</auction.type>"
            "<businessType>A62</businessType>"
            f'<in_Domain.mRID codingScheme="A01">{eic}</in_Domain.mRID>'
            f'<out_Domain.mRID codingScheme="A01">{eic}</out_Domain.mRID>'
            "<contract_MarketAgreement.type>A01</contract_MarketAgreement.type>"
            "<currency_Unit.name>EUR</currency_Unit.name>"
            "<price_Measure_Unit.name>MWH</price_Measure_Unit.name>"
            f"{sequence}<curveType>A03</curveType>"
            f"<Period><timeInterval><start>{_t(p.start)}</start><end>{_t(p.end)}</end>"
            f"</timeInterval><resolution>{p.resolution}</resolution>{''.join(points)}</Period>"
            "</TimeSeries>"
        )
    first = min(p.start for p in periods)
    last = max(p.end for p in periods)
    return (
        f'<?xml version="1.0" encoding="utf-8"?><Publication_MarketDocument xmlns="{DOCUMENT}">'
        "<mRID>05e8314f237d4998b65a04c538fc8ec9</mRID><revisionNumber>1</revisionNumber>"
        "<type>A44</type><createdDateTime>2026-10-06T11:07:18Z</createdDateTime>"
        f"<period.timeInterval><start>{_t(first)}</start><end>{_t(last)}</end>"
        f"</period.timeInterval>{''.join(series)}</Publication_MarketDocument>"
    )


def acknowledgement(code: str, text: str) -> str:
    return (
        f'<?xml version="1.0" encoding="utf-8"?><Acknowledgement_MarketDocument xmlns="{ACK}">'
        "<mRID>1</mRID><createdDateTime>2026-10-07T10:00:00Z</createdDateTime>"
        f"<Reason><code>{code}</code><text>{text}</text></Reason>"
        "</Acknowledgement_MarketDocument>"
    )


@dataclass
class FakeEntsoE:
    periods: list[Period] = field(default_factory=list)
    eic: str = SE3
    asked: list[dict[str, str]] = field(default_factory=list)
    rates: dict[date, dict[str, float]] = field(default_factory=dict)
    rates_asked: int = 0

    async def prices(self, request: web.Request) -> web.Response:
        query = dict(request.query)
        self.asked.append(query)
        if query.get("securityToken") != TOKEN:
            body = acknowledgement("999", "Authentication failed.")
            return web.Response(status=401, text=body, content_type="text/xml")
        start = datetime.strptime(query["periodStart"], "%Y%m%d%H%M").replace(tzinfo=UTC)
        end = datetime.strptime(query["periodEnd"], "%Y%m%d%H%M").replace(tzinfo=UTC)
        found = [p for p in self.periods if p.start < end and p.end > start]
        if not found:
            body = acknowledgement("999", "No matching data found for Data item ENERGY_PRICES")
            return web.Response(text=body, content_type="text/xml")
        return web.Response(text=document(self.eic, found), content_type="text/xml")

    async def ecb(self, request: web.Request) -> web.Response:
        self.rates_asked += 1
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
            "<gesmes:subject>Reference rates</gesmes:subject><gesmes:Sender>"
            "<gesmes:name>European Central Bank</gesmes:name></gesmes:Sender>"
            f"<Cube>{days}</Cube></gesmes:Envelope>"
        )
        return web.Response(text=body, content_type="text/xml")


@asynccontextmanager
async def running(fake: FakeEntsoE) -> AsyncIterator[tuple[str, str]]:
    """Serve the fakes; yields the API's URL and the ECB file's."""
    app = web.Application()
    app.router.add_get("/api", fake.prices)
    app.router.add_get("/eurofxref-hist-90d.xml", fake.ecb)
    server = TestServer(app, host="127.0.0.1")
    await server.start_server()
    base = f"http://127.0.0.1:{server.port}"
    try:
        yield f"{base}/api", f"{base}/eurofxref-hist-90d.xml"
    finally:
        await server.close()
