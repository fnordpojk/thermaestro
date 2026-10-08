"""The OMIE plugin: Spain's or Portugal's day-ahead prices from OMIE, the Iberian market
operator, which publishes them as a file per day with no account.

The file (`marginalpdbc_YYYYMMDD.1`) is plain text: a header line, then one line per
period, `year;month;day;period;Portugal;Spain;` in EUR per MWh, and a closing `*`. The
periods count from midnight in Spain, per 15 minutes (92 or 100 on the days the clock
changes; an hourly file has 23 to 25), so Portugal's local day starts at period 5. A
day not yet published is a 404.

OMIE's legal notice: public, free information "puede ser utilizada libremente, siempre
que se respete íntegramente su contenido original", and the source must be cited.

Checked against the real file on 2026-10-08 (2026-10-09: 96 periods, equal to REE's).
"""

from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import aiohttp

from .. import ecb
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
from ..store import SpotZone

URL = "https://www.omie.es/es/file-download"
NOTICE = "OMIE's legal notice (www.omie.es/es/aviso-legal), read 2026-10-08"
FILE = "OMIE's marginalpdbc file, read 2026-10-08"
MADRID = ZoneInfo("Europe/Madrid")
COLUMN = {"PT": 4, "ES": 5}
"""Where each zone's price is on a line, counting from 0."""


class OmiePlugin(DayAheadPlugin):
    name = "omie"
    version = "0.1.0"
    root = "omie"
    label = "OMIE"

    def __init__(
        self,
        settings: SpotZone,
        *,
        url: str = URL,
        rates: ecb.Rates | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        if settings.zone not in COLUMN:
            raise ValueError(f"OMIE has no bidding zone {settings.zone!r}")
        self.settings = settings
        self._url = url
        self.converter = ecb.Converter(rates)

    # --- what OMIE supplies ------------------------------------------------------------------

    def zone(self) -> ZoneInfo:
        """OMIE's days are Spain's, for Portugal's prices too."""
        return MADRID

    def publication(self) -> Publication:
        # The files appeared between about 13:30 and 14:30 on the days looked at.
        return Publication(daily_after="13:30", tz="Europe/Madrid")

    def series_infos(self) -> tuple[SeriesInfo, ...]:
        return (
            SeriesInfo(
                id="spot",
                kind="price",
                role="energy.spot",
                covers=Knowledge(value=(), known="verified", basis=FILE),
                unit=f"{self.settings.currency}/kWh",
                vat="excl",
                resolution="PT15M",
                area=self.settings.zone,
                publication=self.publication(),
            ),
        )

    def provider(self) -> Provider:
        attribution = "Day-ahead prices: OMIE (www.omie.es)"
        conditions = ["Used as published: its original content respected in full"]
        if self.settings.currency != "EUR":
            attribution += f"; converted to {self.settings.currency} at the {ecb.SOURCE}"
            conditions.append(ecb.CONDITION)
        return Provider(
            name="OMIE",
            operator=Knowledge(
                value="OMIE, the Iberian electricity market operator",
                known="documented",
                basis=NOTICE,
            ),
            coverage=Knowledge(value="Spain and Portugal", known="verified", basis=FILE),
            access=Access(key=Knowledge(value=False, known="verified", basis=FILE)),
            terms=Terms(
                license=Knowledge(
                    value="public, free information that may be used freely, its original"
                    " content respected in full, citing the source",
                    known="documented",
                    basis=NOTICE,
                ),
                attribution=Knowledge(value=attribution, known="documented", basis=NOTICE),
                conditions=Knowledge(value=tuple(conditions), known="documented", basis=NOTICE),
            ),
        )

    async def fetch(self, session: aiohttp.ClientSession, days: list[date]) -> list[Interval]:
        rows: list[tuple[datetime, datetime, float]] = []
        for day in days:
            rows += await self._day(session, day)
        return await self.converter.intervals(session, rows, self.settings.currency, MADRID)

    async def _day(
        self, session: aiohttp.ClientSession, day: date
    ) -> list[tuple[datetime, datetime, float]]:
        params = {"parents": "marginalpdbc", "filename": f"marginalpdbc_{day:%Y%m%d}.1"}
        async with session.get(self._url, params=params) as answer:
            if answer.status == 404:
                return []  # not published yet
            if answer.status != 200:
                raise SourceError(f"OMIE answered HTTP {answer.status}")
            text = (await answer.read()).decode("latin-1")
        return _rows(text, day, COLUMN[self.settings.zone])


def _rows(text: str, day: date, column: int) -> list[tuple[datetime, datetime, float]]:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines or not lines[0].upper().startswith("MARGINALPDBC"):
        raise SourceError("OMIE sent something other than its prices file")
    found: dict[int, float] = {}
    for line in lines[1:]:
        if line == "*":
            break
        fields = line.split(";")
        try:
            on = date(int(fields[0]), int(fields[1]), int(fields[2]))
            period, price = int(fields[3]), float(fields[column])
        except (IndexError, ValueError):
            raise SourceError(f"a line of OMIE's file can't be read: {line!r}") from None
        if on != day:
            raise SourceError(f"OMIE's file for {day} holds {on}")
        found[period] = price
    midnight = datetime.combine(day, datetime.min.time(), MADRID).astimezone(UTC)
    hours = (
        datetime.combine(day + timedelta(days=1), datetime.min.time(), MADRID).astimezone(UTC)
        - midnight
    ) / timedelta(hours=1)
    step = timedelta(minutes=15) if len(found) > hours else timedelta(hours=1)
    return [
        (midnight + (n - 1) * step, midnight + n * step, price)
        for n, price in sorted(found.items())
    ]


def create(context: PluginContext) -> OmiePlugin:
    """The entry point: a plugin instance from its settings."""
    return OmiePlugin(SpotZone.model_validate(dict(context.settings)))
