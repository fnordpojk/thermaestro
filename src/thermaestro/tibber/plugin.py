"""The Tibber plugin: a home's prices per 15 minutes, from Tibber's GraphQL API.

It logs in with a personal access token from the secrets file and asks for prices only:
the home's id and time zone, and today's and tomorrow's prices. Never the name, address,
contact or consumption fields.

Two series, both per 15 minutes:
- `energy`: the spot price, without VAT (Tibber's `energy`, "Nord Pool spot price");
- `total`: what Tibber charges per kWh, with VAT (Tibber's `total`). It holds the spot
  price, Tibber's own adders and the VAT on both, so the price stack takes it either as
  the supplier's layer on its own, or as a check on a stack built from `energy`.

Tibber's schema says `tax` includes the energy tax in Sweden; a Swedish home's prices
show otherwise (2026-10-07: tax was 25 % of the spot price plus a fixed adder). In Sweden
the energy tax is billed with the grid fees, so it is a layer of its own.
"""

from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

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
from ..store import SecretStore, Tibber

URL = "https://api.tibber.com/v1-beta/gql"
QUARTER = timedelta(minutes=15)
SCHEMA = "Tibber API schema, read 2026-10-07"
CHECKED = "a Swedish (SE3) home's prices, 2026-10-07"

QUERY = """
query Prices {
  viewer {
    homes {
      id
      timeZone
      currentSubscription {
        priceInfo(resolution: QUARTER_HOURLY) {
          today { total energy startsAt currency }
          tomorrow { total energy startsAt currency }
        }
      }
    }
  }
}
"""


class TibberPlugin(DayAheadPlugin):
    name = "tibber"
    version = "0.1.0"
    root = "tibber"
    label = "Tibber"

    def __init__(
        self,
        settings: Tibber,
        *,
        secrets: SecretStore | None = None,
        url: str = URL,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self.settings = settings
        self._secrets = secrets
        self._url = url
        self._zone: ZoneInfo | None = None
        self.currency: str | None = None

    # --- what Tibber supplies ---------------------------------------------------------------

    def zone(self) -> ZoneInfo:
        return self._zone or ZoneInfo("UTC")

    def publication(self) -> Publication:
        # Nord Pool's day-ahead results come out shortly before 13:00 CET.
        return Publication(daily_after="13:00", tz=self.zone().key)

    def series_infos(self) -> tuple[SeriesInfo, ...]:
        if self.currency is None:
            return ()
        unit = f"{self.currency}/kWh"
        known = "verified" if self.currency == "SEK" else "documented"
        return (
            SeriesInfo(
                id="energy",
                kind="price",
                role="energy.spot",
                covers=Knowledge(
                    value=(), known=known, basis=(f"{SCHEMA}: Nord Pool spot price", CHECKED)
                ),
                unit=unit,
                vat="excl",
                resolution="PT15M",
                publication=self.publication(),
            ),
            SeriesInfo(
                id="total",
                kind="price",
                role="energy.supplier",
                covers=Knowledge(
                    value=("energy.spot", "vat"),
                    known=known,
                    basis=(
                        f"{SCHEMA}: total is energy plus tax; tax is the guarantee of origin"
                        " certificate, energy tax (Sweden only) and VAT",
                        f"{CHECKED}: tax was 25 % of energy plus a fixed adder, no energy tax",
                    ),
                ),
                unit=unit,
                vat="incl",
                resolution="PT15M",
                publication=self.publication(),
            ),
        )

    def provider(self) -> Provider:
        return Provider(
            name="Tibber",
            coverage=Knowledge(
                value="the homes on the Tibber account the token belongs to",
                known="documented",
                basis=SCHEMA,
            ),
            access=Access(
                key=Knowledge(
                    value=True,
                    known="documented",
                    basis="a personal access token from developer.tibber.com",
                ),
            ),
            terms=Terms(
                storing_allowed=Knowledge(
                    value=True,
                    known="documented",
                    basis=f"{SCHEMA}: prices never change, so store fetched prices locally",
                ),
            ),
        )

    async def fetch(self, session: aiohttp.ClientSession, days: list[date]) -> list[Interval]:
        """Today's and tomorrow's prices, whatever days are asked for: that is what
        Tibber's `priceInfo` gives."""
        token = await self._token()
        async with session.post(
            self._url,
            json={"query": QUERY},
            headers={"Authorization": f"Bearer {token}"},
        ) as answer:
            if answer.status in (401, 403):
                raise Refused("Tibber refused the token; make a new one at developer.tibber.com")
            if answer.status != 200:
                raise SourceError(f"Tibber answered HTTP {answer.status}")
            data = await answer.json(content_type=None)
        if not isinstance(data, dict):
            raise SourceError("Tibber's answer isn't a GraphQL result")
        errors = data.get("errors") or []
        if any((e.get("extensions") or {}).get("code") == "UNAUTHENTICATED" for e in errors):
            raise Refused("Tibber refused the token; make a new one at developer.tibber.com")
        if errors:
            raise SourceError("; ".join(str(e.get("message")) for e in errors))
        home = self._home(((data.get("data") or {}).get("viewer") or {}).get("homes") or [])
        try:
            self._zone = ZoneInfo(home.get("timeZone") or "UTC")
        except (ZoneInfoNotFoundError, ValueError):
            raise SourceError(f"unknown time zone {home.get('timeZone')!r}") from None
        info = home["currentSubscription"].get("priceInfo") or {}
        prices = [*(info.get("today") or []), *(info.get("tomorrow") or [])]
        return self._intervals(prices)

    async def _token(self) -> str:
        if self._secrets is None:
            raise Refused("no secrets to take the Tibber token from")
        secret = await self._secrets.get(self.settings.token)
        if secret is None:
            raise Refused(f"the token {self.settings.token!r} isn't in the secrets")
        return secret.get_secret_value()

    def _home(self, homes: list[dict[str, Any]]) -> dict[str, Any]:
        priced = [h for h in homes if h.get("currentSubscription")]
        if self.settings.home is not None:
            chosen = [h for h in homes if h.get("id") == self.settings.home]
            if not chosen:
                raise Refused("the chosen home isn't on this Tibber account")
            if not chosen[0].get("currentSubscription"):
                raise Refused("the chosen home has no current Tibber contract")
            return chosen[0]
        if not priced:
            raise Refused("no home on this Tibber account has a current contract")
        return priced[0]

    def _intervals(self, prices: list[dict[str, Any]]) -> list[Interval]:
        try:
            starts = [datetime.fromisoformat(p["startsAt"]) for p in prices]
            rows = sorted(zip(starts, prices, strict=True), key=lambda row: row[0])
            currencies = {p["currency"] for p in prices}
        except (KeyError, TypeError, ValueError) as e:
            raise SourceError(f"a price Tibber sent can't be read ({e})") from None
        if len(currencies) > 1:
            raise SourceError(f"prices in several currencies: {sorted(currencies)}")
        if currencies:
            self.currency = currencies.pop()
        elif self.currency is None:
            # No prices at all, as for a contract not yet started: the currency is still
            # unknown, and so are the series.
            raise SourceError("Tibber sent no prices")
        out = []
        for n, (start, price) in enumerate(rows):
            end = rows[n + 1][0] if n + 1 < len(rows) else start + QUARTER
            if end - start > QUARTER:
                end = start + QUARTER  # a gap: the next quarter's price is missing
            for series, field, vat in (("energy", "energy", "excl"), ("total", "total", "incl")):
                value = price.get(field)
                if value is None:
                    continue
                out.append(
                    Interval.model_validate(
                        {
                            "series": series,
                            "start": start,
                            "end": end,
                            "value": value,
                            "unit": f"{self.currency}/kWh",
                            "vat": vat,
                            "status": "final",
                        }
                    )
                )
        return out


def create(context: PluginContext) -> TibberPlugin:
    """The entry point: a plugin instance from its settings."""
    return TibberPlugin(Tibber.model_validate(dict(context.settings)), secrets=context.secrets)
