"""The bidding zones of the European day-ahead market, as the price sources name them:
each zone's EIC code (ENTSO-E's name for it), its country, the currency used there, and
its time zone (where its days begin and end)."""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Zone:
    eic: str
    country: str
    """ISO 3166 code, for the country's name in the user's language."""
    currency: str
    tz: str


def _zones() -> dict[str, Zone]:
    def z(eic: str, country: str, currency: str, tz: str) -> Zone:
        return Zone(eic, country, currency, tz)

    return {
        "AT": z("10YAT-APG------L", "AT", "EUR", "Europe/Vienna"),
        "BE": z("10YBE----------2", "BE", "EUR", "Europe/Brussels"),
        "BG": z("10YCA-BULGARIA-R", "BG", "EUR", "Europe/Sofia"),
        "CH": z("10YCH-SWISSGRIDZ", "CH", "CHF", "Europe/Zurich"),
        "CZ": z("10YCZ-CEPS-----N", "CZ", "CZK", "Europe/Prague"),
        "DE-LU": z("10Y1001A1001A82H", "DE", "EUR", "Europe/Berlin"),
        "DK1": z("10YDK-1--------W", "DK", "DKK", "Europe/Copenhagen"),
        "DK2": z("10YDK-2--------M", "DK", "DKK", "Europe/Copenhagen"),
        "EE": z("10Y1001A1001A39I", "EE", "EUR", "Europe/Tallinn"),
        "ES": z("10YES-REE------0", "ES", "EUR", "Europe/Madrid"),
        "FI": z("10YFI-1--------U", "FI", "EUR", "Europe/Helsinki"),
        "FR": z("10YFR-RTE------C", "FR", "EUR", "Europe/Paris"),
        "GR": z("10YGR-HTSO-----Y", "GR", "EUR", "Europe/Athens"),
        "HR": z("10YHR-HEP------M", "HR", "EUR", "Europe/Zagreb"),
        "HU": z("10YHU-MAVIR----U", "HU", "HUF", "Europe/Budapest"),
        "IT-CALA": z("10Y1001C--00096J", "IT", "EUR", "Europe/Rome"),
        "IT-CNOR": z("10Y1001A1001A70O", "IT", "EUR", "Europe/Rome"),
        "IT-CSUD": z("10Y1001A1001A71M", "IT", "EUR", "Europe/Rome"),
        "IT-NORD": z("10Y1001A1001A73I", "IT", "EUR", "Europe/Rome"),
        "IT-SARD": z("10Y1001A1001A74G", "IT", "EUR", "Europe/Rome"),
        "IT-SICI": z("10Y1001A1001A75E", "IT", "EUR", "Europe/Rome"),
        "IT-SUD": z("10Y1001A1001A788", "IT", "EUR", "Europe/Rome"),
        "LT": z("10YLT-1001A0008Q", "LT", "EUR", "Europe/Vilnius"),
        "LV": z("10YLV-1001A00074", "LV", "EUR", "Europe/Riga"),
        "NL": z("10YNL----------L", "NL", "EUR", "Europe/Amsterdam"),
        "NO1": z("10YNO-1--------2", "NO", "NOK", "Europe/Oslo"),
        "NO2": z("10YNO-2--------T", "NO", "NOK", "Europe/Oslo"),
        "NO3": z("10YNO-3--------J", "NO", "NOK", "Europe/Oslo"),
        "NO4": z("10YNO-4--------9", "NO", "NOK", "Europe/Oslo"),
        "NO5": z("10Y1001A1001A48H", "NO", "NOK", "Europe/Oslo"),
        "PL": z("10YPL-AREA-----S", "PL", "PLN", "Europe/Warsaw"),
        "PT": z("10YPT-REN------W", "PT", "EUR", "Europe/Lisbon"),
        "RO": z("10YRO-TEL------P", "RO", "RON", "Europe/Bucharest"),
        "SE1": z("10Y1001A1001A44P", "SE", "SEK", "Europe/Stockholm"),
        "SE2": z("10Y1001A1001A45N", "SE", "SEK", "Europe/Stockholm"),
        "SE3": z("10Y1001A1001A46L", "SE", "SEK", "Europe/Stockholm"),
        "SE4": z("10Y1001A1001A47J", "SE", "SEK", "Europe/Stockholm"),
        "SI": z("10YSI-ELES-----O", "SI", "EUR", "Europe/Ljubljana"),
        "SK": z("10YSK-SEPS-----K", "SK", "EUR", "Europe/Bratislava"),
    }


ZONES = _zones()
