"""The Energi Data Service plugin: a Danish household's grid tariffs from Energinet's open
data, with no account.

DataHub's price list (`DatahubPricelist`) holds every grid company's charges, each a row
with a validity period and 24 prices, one per hour of the Danish day (Price1 is
00:00 to 01:00; a flat rate has only Price1). On the days the clock changes, the hour that is
added or left out has the price of the hour before. Rows are matched by the grid company's
GLN number and charge codes, since the companies' names changed with DataHub 3.0.

Three series, each in DKK/kWh without VAT:
- `grid`: the household's grid company tariff ("Nettarif C"), with any rebate on it
  ("Rabat Nettarif C", negative while it applies);
- `energinet`: Energinet's transmission and system tariffs;
- `elafgift`: the electricity tax.

The household picks its grid company from the list (`companies`), so nothing about the
house is sent. Checked against the live data on 2026-10-10.
"""

import json
from collections.abc import Iterable, Mapping
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import aiohttp

from ..cap.model import (
    Access,
    Interval,
    Knowledge,
    Provider,
    Publication,
    SeriesInfo,
    Terms,
)
from ..core.plugins import PluginContext
from ..dayahead import DayAheadPlugin, SourceError
from ..store.settings import DanishGrid

URL = "https://api.energidataservice.dk/dataset/DatahubPricelist"
COPENHAGEN = ZoneInfo("Europe/Copenhagen")
ENERGINET = "5790000432752"
TRANSMISSION, SYSTEM, ELAFGIFT = "40000", "41000", "EA-001"
TARIFFS = ("Nettarif C", "Nettarif C time")
REBATE = "Rabat Nettarif C"
BACK = timedelta(days=3 * 366)
"""How far back rows may start and still apply: a tariff can stay unchanged for years."""
DATASET = "Energi Data Service, DatahubPricelist and its metadata, read 2026-10-10"
TERMS = "Energi Data Service's terms of use: CC BY 4.0, read 2026-10-08"


class EnergiDataServicePlugin(DayAheadPlugin):
    name = "energidataservice"
    version = "0.1.0"
    root = "energidataservice"
    label = "Energi Data Service"

    def __init__(self, settings: DanishGrid, *, url: str = URL, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.settings = settings
        self._url = url

    def zone(self) -> ZoneInfo:
        return COPENHAGEN

    def publication(self) -> Publication:
        # "updated daily in the morning (usually before 9 am local Danish time)"
        return Publication(daily_after="09:00", tz="Europe/Copenhagen")

    def series_infos(self) -> tuple[SeriesInfo, ...]:
        def info(id: str, role: str) -> SeriesInfo:
            return SeriesInfo(
                id=id,
                kind="price",
                role=role,
                covers=Knowledge(value=(), known="verified", basis=DATASET),
                unit="DKK/kWh",
                vat="excl",
                resolution="PT1H",
                area="DK",
                publication=self.publication(),
            )

        return (
            info("grid", "grid.tou"),
            info("energinet", "grid.transfer"),
            info("elafgift", "tax.energy"),
        )

    def provider(self) -> Provider:
        return Provider(
            name="Energi Data Service",
            operator=Knowledge(
                value="Energinet, Denmark's transmission system operator",
                known="documented",
                basis=DATASET,
            ),
            coverage=Knowledge(value="Denmark", known="verified", basis=DATASET),
            access=Access(key=Knowledge(value=False, known="verified", basis=DATASET)),
            terms=Terms(
                license=Knowledge(value="CC BY 4.0", known="documented", basis=TERMS),
                attribution=Knowledge(
                    value="Grid tariffs: Energi Data Service (Energinet)",
                    known="documented",
                    basis=TERMS,
                ),
            ),
        )

    async def fetch(self, session: aiohttp.ClientSession, days: list[date]) -> list[Interval]:
        today = min(days)
        own = await rows(
            session,
            self._url,
            {"GLN_Number": [self.settings.gln], "ChargeTypeCode": list(self.settings.codes)},
            today,
        )
        national = await rows(
            session,
            self._url,
            {"GLN_Number": [ENERGINET], "ChargeTypeCode": [TRANSMISSION, SYSTEM, ELAFGIFT]},
            today,
        )
        energinet = [r for r in national if r.get("ChargeTypeCode") in (TRANSMISSION, SYSTEM)]
        tax = [r for r in national if r.get("ChargeTypeCode") == ELAFGIFT]
        out: list[Interval] = []
        for day in days:
            out += hours(day, "grid", own)
            out += hours(day, "energinet", energinet)
            out += hours(day, "elafgift", tax)
        return out


async def rows(
    session: aiohttp.ClientSession, url: str, where: Mapping[str, list[str]], today: date
) -> list[dict[str, Any]]:
    """The price list's rows that match, from those starting up to three years back."""
    params = {
        "filter": json.dumps(where),
        "start": (today - BACK).isoformat(),
        "limit": "2000",
    }
    async with session.get(url, params=params) as answer:
        if answer.status != 200:
            raise SourceError(f"Energi Data Service answered HTTP {answer.status}")
        body = await answer.json(content_type=None)
    found = body.get("records") if isinstance(body, dict) else None
    if not isinstance(found, list):
        raise SourceError("Energi Data Service sent something other than records")
    return [r for r in found if isinstance(r, dict)]


def _local(text: object) -> datetime | None:
    return datetime.fromisoformat(text) if isinstance(text, str) else None


def _valid(row: Mapping[str, Any], at: datetime) -> bool:
    """Whether a row applies at a Danish local time (its dates are local, without zone)."""
    start, end = _local(row.get("ValidFrom")), _local(row.get("ValidTo"))
    return start is not None and start <= at and (end is None or at < end)


def _price(row: Mapping[str, Any], hour: int) -> float | None:
    hourly = row.get("ResolutionDuration") == "PT1H"
    value = row.get(f"Price{hour + 1}" if hourly else "Price1")
    if value is None and hourly:
        value = row.get("Price1")
    return float(value) if isinstance(value, int | float) else None


def hours(day: date, series: str, found: Iterable[Mapping[str, Any]]) -> list[Interval]:
    """A day's hours of a series: the sum of the rows that apply in each, where all give a
    price. An hour no row applies to is left out: what it costs isn't known."""
    found = list(found)
    start = datetime.combine(day, datetime.min.time(), COPENHAGEN).astimezone(UTC)
    end = datetime.combine(day + timedelta(days=1), datetime.min.time(), COPENHAGEN)
    out = []
    t = start
    while t < end.astimezone(UTC):
        local = t.astimezone(COPENHAGEN).replace(tzinfo=None)
        applying = [r for r in found if _valid(r, local)]
        prices = [_price(r, local.hour) for r in applying]
        if applying and all(p is not None for p in prices):
            out.append(
                Interval(
                    series=series,
                    start=t,
                    end=t + timedelta(hours=1),
                    value=round(sum(p for p in prices if p is not None), 6),
                    unit="DKK/kWh",
                    vat="excl",
                    status="final",
                    source="calculated" if len(applying) > 1 else None,
                    why=" + ".join(str(r.get("ChargeTypeCode")) for r in applying)
                    if len(applying) > 1
                    else None,
                )
            )
        t += timedelta(hours=1)
    return out


async def companies(
    session: aiohttp.ClientSession, today: date, url: str = URL
) -> list[dict[str, Any]]:
    """The grid companies with a household tariff that applies today, each with its GLN
    number, name and the codes to ask for: its tariff and any rebate on it. Sorted by name;
    a company with two household tariffs is listed once for each."""
    found = await rows(session, url, {"Note": [*TARIFFS, REBATE]}, today)
    now = datetime.combine(today, datetime.min.time())
    tariffs: dict[tuple[str, str], dict[str, Any]] = {}
    rebates: dict[str, list[str]] = {}
    for row in found:
        if not _valid(row, now):
            continue
        gln, code = str(row.get("GLN_Number") or ""), str(row.get("ChargeTypeCode") or "")
        if not gln or not code:
            continue
        if row.get("Note") == REBATE:
            rebates.setdefault(gln, []).append(code)
        else:
            name = str(row.get("ChargeOwner") or gln)
            tariffs[(gln, code)] = {
                "gln": gln,
                "company": name,
                "tariff": code,
                "note": row["Note"],
            }
    out = [
        {**t, "codes": [t["tariff"], *sorted(set(rebates.get(gln, [])))]}
        for (gln, _), t in tariffs.items()
    ]
    return sorted(out, key=lambda c: (c["company"].lower(), c["tariff"]))


def create(context: PluginContext) -> EnergiDataServicePlugin:
    """The entry point: a plugin instance from its settings."""
    return EnergiDataServicePlugin(DanishGrid.model_validate(dict(context.settings)))
