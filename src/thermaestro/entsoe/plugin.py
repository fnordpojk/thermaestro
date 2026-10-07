"""The ENTSO-E plugin: a bidding zone's day-ahead prices from the Transparency Platform.

Asks the platform's API (document type A44, day-ahead) with the user's own security token
for today and tomorrow, as local days of the zone. Prices come per MWh in euros; they
are given per kWh, and in another currency converted at the ECB's reference rates.

Reading the answer:
- curve type A03 (what the platform sends): a point only where the price changes; it
  holds until the next point, the last one to the end of the period;
- several time series for one zone and day: the one with sequence 1 is the day-ahead
  coupling's (in DE-LU a second auction's prices are sequence 2), and where series of
  several resolutions cover the same time, the finest is used;
- "No matching data found" (reason 999) is an answer, not a failure: nothing published.

Built from the platform's documentation; it hasn't been run against the real API yet.
"""

import re
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
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
from ..dayahead import DayAheadPlugin, Refused, SourceError
from ..store import EntsoE, SecretStore
from .ecb import SOURCE as ECB_SOURCE
from .ecb import Rates
from .zones import ZONES

URL = "https://web-api.tp.entsoe.eu/api"
DOCS = "ENTSO-E Transparency Platform API documentation, read 2026-10-07"
RATES_FRESH_S = 6 * 3600.0
SEQUENCE_FIRST = ("DE-LU", "AT")
"""Zones whose answer also holds another auction's prices, so sequence 1 is asked for."""


@dataclass
class _Block:
    sequence: int | None
    step: timedelta
    intervals: list[tuple[datetime, datetime, float]]
    """Start, end, and the price in EUR per MWh."""


class EntsoEPlugin(DayAheadPlugin):
    name = "entsoe"
    version = "0.1.0"
    root = "entsoe"
    label = "ENTSO-E"

    def __init__(
        self,
        settings: EntsoE,
        *,
        secrets: SecretStore | None = None,
        url: str = URL,
        rates: Rates | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        if settings.zone not in ZONES:
            raise ValueError(f"no bidding zone {settings.zone!r}")
        self.settings = settings
        self.area = ZONES[settings.zone]
        self._secrets = secrets
        self._url = url
        self.rates = rates or Rates()
        self._rates_loaded: float | None = None

    # --- what ENTSO-E supplies --------------------------------------------------------------

    def zone(self) -> ZoneInfo:
        return ZoneInfo(self.area.tz)

    def publication(self) -> Publication:
        # The day-ahead auction closes at 12:00 CET and its results come out at about
        # 12:57; the platform publishes them within the hour.
        return Publication(daily_after="13:00", tz="Europe/Brussels")

    def series_infos(self) -> tuple[SeriesInfo, ...]:
        return (
            SeriesInfo(
                id="spot",
                kind="price",
                role="energy.spot",
                covers=Knowledge(value=(), known="documented", basis=DOCS),
                unit=f"{self.settings.currency}/kWh",
                vat="excl",
                resolution="PT15M",
                area=self.settings.zone,
                publication=self.publication(),
            ),
        )

    def provider(self) -> Provider:
        converted = self.settings.currency != "EUR"
        attribution = "Day-ahead prices: ENTSO-E Transparency Platform"
        if converted:
            attribution += f"; converted to {self.settings.currency} at the {ECB_SOURCE}"
        conditions = [
            "Energy prices aren't on the platform's list of data free to reuse under CC-BY"
            " 4.0; its Terms of Use apply, and the power exchanges that produce the prices"
            " may hold rights in them",
        ]
        if converted:
            conditions.append(
                "The ECB's rates are for information only; a price converted with them must say so"
            )
        return Provider(
            name="ENTSO-E Transparency Platform",
            operator=Knowledge(value="ENTSO-E", known="documented", basis=DOCS),
            coverage=Knowledge(
                value="the bidding zones of the European day-ahead market",
                known="documented",
                basis=DOCS,
            ),
            access=Access(
                key=Knowledge(
                    value=True,
                    known="documented",
                    basis=f"{DOCS}: a security token, given on request by the platform",
                ),
                rate_limit=Knowledge(
                    value="400 requests a minute per token", known="documented", basis=DOCS
                ),
            ),
            terms=Terms(
                license=Knowledge(
                    value="ENTSO-E Transparency Platform Terms of Use (2023)",
                    known="documented",
                    basis=DOCS,
                ),
                attribution=Knowledge(value=attribution, known="documented", basis=DOCS),
                conditions=Knowledge(value=tuple(conditions), known="documented", basis=DOCS),
            ),
        )

    async def fetch(self, session: aiohttp.ClientSession, days: list[date]) -> list[Interval]:
        zone = self.zone()
        start = datetime.combine(min(days), datetime.min.time(), zone).astimezone(UTC)
        end = datetime.combine(max(days) + timedelta(days=1), datetime.min.time(), zone)
        params = {
            "securityToken": await self._token(),
            "documentType": "A44",
            "in_Domain": self.area.eic,
            "out_Domain": self.area.eic,
            "periodStart": start.strftime("%Y%m%d%H%M"),
            "periodEnd": end.astimezone(UTC).strftime("%Y%m%d%H%M"),
            "contract_MarketAgreement.type": "A01",
        }
        if self.settings.zone in SEQUENCE_FIRST:
            params["classificationSequence_AttributeInstanceComponent.position"] = "1"
        try:
            async with session.get(self._url, params=params) as answer:
                status, body = answer.status, await answer.read()
        except aiohttp.ClientError as e:
            # Never the error's own text: it can quote the URL, and so the token.
            raise SourceError(f"ENTSO-E can't be reached ({type(e).__name__})") from None
        if status == 401:
            raise Refused(
                "ENTSO-E refused the security token; check it, or ask the platform for a new one"
            )
        if status == 429:
            raise SourceError("ENTSO-E: too many requests")
        if status != 200:
            raise SourceError(f"ENTSO-E answered HTTP {status}: {_reason(body)}")
        return await self._intervals(session, _parse(body, self.area.eic))

    async def _token(self) -> str:
        if self._secrets is None:
            raise Refused("no secrets to take the ENTSO-E token from")
        secret = await self._secrets.get(self.settings.token)
        if secret is None:
            raise Refused(f"the token {self.settings.token!r} isn't in the secrets")
        return secret.get_secret_value()

    async def _intervals(
        self, session: aiohttp.ClientSession, blocks: list[_Block]
    ) -> list[Interval]:
        chosen = _choose(blocks)
        currency = self.settings.currency
        zone = self.zone()
        out = []
        for start, end, eur_mwh in chosen:
            value = eur_mwh / 1000
            extra: dict[str, Any] = {}
            if currency != "EUR":
                day = start.astimezone(zone).date()
                rate = await self._rate(session, day, currency)
                value *= rate[1]
                extra = {
                    "source": "calculated",
                    "why": f"converted from EUR at the ECB's rate of {rate[0]}: {rate[1]}",
                }
            out.append(
                Interval.model_validate(
                    {
                        "series": "spot",
                        "start": start,
                        "end": end,
                        "value": round(value, 6),
                        "unit": f"{currency}/kWh",
                        "vat": "excl",
                        "status": "final",
                        **extra,
                    }
                )
            )
        return out

    async def _rate(
        self, session: aiohttp.ClientSession, day: date, currency: str
    ) -> tuple[date, float]:
        old = self._rates_loaded is None or time.monotonic() - self._rates_loaded > RATES_FRESH_S
        found = None if old else self.rates.for_day(day, currency)
        if found is None:
            try:
                await self.rates.load(session)
            except aiohttp.ClientError as e:
                raise SourceError(f"the ECB can't be reached ({type(e).__name__})") from None
            self._rates_loaded = time.monotonic()
            found = self.rates.for_day(day, currency)
        if found is None:
            raise SourceError(f"the ECB has no {currency} rate for {day}'s prices")
        return found


# --- the answer ------------------------------------------------------------------------------


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _child(element: ET.Element, name: str) -> ET.Element | None:
    return next((c for c in element if _local(c.tag) == name), None)


def _text(element: ET.Element, name: str) -> str | None:
    found = _child(element, name)
    return found.text.strip() if found is not None and found.text else None


def _root(body: bytes) -> ET.Element:
    # Python's expat refuses entity expansion bombs and ElementTree doesn't fetch
    # external entities, so this answer needs no other XML parser.
    try:
        return ET.fromstring(body)  # noqa: S314
    except ET.ParseError as e:
        raise SourceError(f"ENTSO-E's answer can't be read ({e})") from None


def _reason(body: bytes) -> str:
    try:
        root = _root(body)
    except SourceError:
        return "no reason given"
    reason = next((e for e in root.iter() if _local(e.tag) == "Reason"), None)
    return (_text(reason, "text") if reason is not None else None) or "no reason given"


STEP = re.compile(r"^PT(?:(\d+)H)?(?:(\d+)M)?$")


def _step(resolution: str | None) -> timedelta:
    match = STEP.match(resolution or "")
    if match is None or not any(match.groups()):
        raise SourceError(f"a resolution that can't be read: {resolution!r}")
    hours, minutes = (int(g) if g else 0 for g in match.groups())
    return timedelta(hours=hours, minutes=minutes)


def _when(text: str | None) -> datetime:
    if text is None:
        raise SourceError("a period without its start or end")
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        raise SourceError(f"a time that can't be read: {text!r}") from None


def _parse(body: bytes, eic: str) -> list[_Block]:
    root = _root(body)
    if _local(root.tag) == "Acknowledgement_MarketDocument":
        reason = next((e for e in root.iter() if _local(e.tag) == "Reason"), None)
        code = _text(reason, "code") if reason is not None else None
        text = _text(reason, "text") if reason is not None else None
        if code == "999" and text and "no matching data" in text.lower():
            return []
        raise SourceError(f"ENTSO-E: {text or 'refused without a reason'}")
    if _local(root.tag) != "Publication_MarketDocument":
        raise SourceError(f"ENTSO-E sent a {_local(root.tag)}, not prices")
    blocks = []
    for series in (e for e in root if _local(e.tag) == "TimeSeries"):
        contract = _text(series, "contract_MarketAgreement.type")
        if contract not in (None, "A01") or _text(series, "in_Domain.mRID") not in (None, eic):
            continue
        if (_text(series, "currency_Unit.name") or "EUR") != "EUR":
            raise SourceError(f"prices in {_text(series, 'currency_Unit.name')}, not EUR")
        if (_text(series, "price_Measure_Unit.name") or "MWH") != "MWH":
            raise SourceError(f"prices per {_text(series, 'price_Measure_Unit.name')}")
        sequence_text = _text(series, "classificationSequence_AttributeInstanceComponent.position")
        sequence = int(sequence_text) if sequence_text and sequence_text.isdigit() else None
        variable = (_text(series, "curveType") or "A01") == "A03"
        for period in (e for e in series if _local(e.tag) == "Period"):
            blocks.append(_period(period, sequence, variable))
    return blocks


def _period(period: ET.Element, sequence: int | None, variable: bool) -> _Block:
    interval = _child(period, "timeInterval")
    if interval is None:
        raise SourceError("a period without its time interval")
    start, end = _when(_text(interval, "start")), _when(_text(interval, "end"))
    step = _step(_text(period, "resolution"))
    points: list[tuple[int, float]] = []
    for point in (e for e in period if _local(e.tag) == "Point"):
        try:
            points.append(
                (int(_text(point, "position") or ""), float(_text(point, "price.amount") or ""))
            )
        except ValueError:
            raise SourceError("a point that can't be read") from None
    points.sort()
    out = []
    for n, (position, price) in enumerate(points):
        first = start + (position - 1) * step
        if variable:
            # A03: the price holds until the next point's position, the last one to the end.
            until = start + (points[n + 1][0] - 1) * step if n + 1 < len(points) else end
        else:
            until = first + step
        t = first
        while t < min(until, end):
            out.append((t, t + step, price))
            t += step
    return _Block(sequence, step, out)


def _choose(blocks: list[_Block]) -> list[tuple[datetime, datetime, float]]:
    """Sequence 1 where there is one; then, for each time, the finest resolution."""
    if any(b.sequence == 1 for b in blocks):
        blocks = [b for b in blocks if b.sequence == 1]
    out: list[tuple[datetime, datetime, float]] = []
    covered: list[tuple[datetime, datetime]] = []
    for block in sorted(blocks, key=lambda b: b.step):
        for start, end, price in block.intervals:
            if any(s < end and start < e for s, e in covered):
                continue
            out.append((start, end, price))
        covered += [(s, e) for s, e, _ in block.intervals]
    return sorted(out)


def create(context: PluginContext) -> EntsoEPlugin:
    """The entry point: a plugin instance from its settings."""
    return EntsoEPlugin(EntsoE.model_validate(dict(context.settings)), secrets=context.secrets)
