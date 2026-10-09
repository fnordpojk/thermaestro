"""The house's days: its time zone, and the public holidays weekly patterns may treat as
another day of the week."""

import inspect
from collections.abc import Callable
from datetime import date, datetime, tzinfo
from functools import lru_cache
from typing import Any
from zoneinfo import ZoneInfo

import holidays


@lru_cache(maxsize=32)
def _holidays(code: str, year: int) -> frozenset[date]:
    country, _, region = code.partition("-")
    found = holidays.country_holidays(country, subdiv=region or None, years=year)
    accepts = inspect.signature(type(found).__init__).parameters
    if "include_sundays" in accepts:  # Sweden lists every Sunday too: they are Sundays anyway
        options: dict[str, Any] = {"include_sundays": False}
        found = type(found)(years=year, subdiv=region or None, **options)
    return frozenset(found)


class Calendar:
    def __init__(
        self, zone: tzinfo | str = "UTC", holidays: str | None = None, holidays_as: int | None = 6
    ) -> None:
        self.zone = ZoneInfo(zone) if isinstance(zone, str) else zone
        self.holidays = holidays
        self.holidays_as = holidays_as

    def local(self, at: datetime) -> datetime:
        return at.astimezone(self.zone)

    def holiday(self, day: date) -> bool:
        return self.holidays is not None and day in _holidays(self.holidays, day.year)

    def weekday(self, day: date) -> int:
        """The day of the week a date counts as: a public holiday as the chosen day."""
        if self.holidays_as is not None and self.holiday(day):
            return self.holidays_as
        return day.weekday()


Holiday = Callable[[date], bool]
