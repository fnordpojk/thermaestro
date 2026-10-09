"""Which source of the spot price suits a bidding zone: the one Setup → Prices picks first,
the one that stands in for it, and the others a household may choose instead.

The rules:
- every zone of a country takes its prices from the same source, so a country's prices
  are all treated alike;
- a source with prices per 15 minutes comes before one with hourly prices; Beneficial
  Apps' Nordic price sites stand in where another source is first;
- a Tibber customer's own prices come first, in the countries Tibber sells in;
- where Energy-Charts' prices are for private use only and nothing else covers the
  country, ENTSO-E comes first once the household has a token, and Energy-Charts stands in;
- ENTSO-E, with the household's own token, stands in last everywhere else.
"""

from dataclasses import dataclass

from .energy_charts.plugin import CC_BY
from .zones import ZONES

TIBBER_COUNTRIES = frozenset({"NO", "SE", "DE", "NL"})
"""Where Tibber sells electricity."""
NORDIC = frozenset({"SE", "NO", "DK", "FI"})
IBERIA = frozenset({"ES", "PT"})


@dataclass(frozen=True, slots=True)
class Choice:
    plugin: str
    series: str
    """The plugin's series that holds the spot price."""
    resolution: str
    private: bool = False
    """Its prices are for private and internal use only."""
    token: bool = False
    """It needs the household's own token."""


def _energy_charts(zone: str) -> Choice:
    return Choice(
        "energy_charts",
        "spot",
        "PT1H" if zone == "CH" else "PT15M",
        private=zone not in CC_BY,
    )


def choices(zone: str, *, tibber: bool = False, entsoe: bool = False) -> list[Choice]:
    """The sources for a zone, in the order to take them: the first is picked, the second
    stands in for it. `tibber` and `entsoe`: whether the household has entered a token."""
    area = ZONES.get(zone)
    if area is None:
        return []
    country = area.country
    nordic_site = Choice("nordic_sites", "spot", "PT15M" if country == "SE" else "PT1H")
    entsoe_choice = Choice("entsoe", "spot", "PT15M", token=True)
    out: list[Choice] = []
    if tibber and country in TIBBER_COUNTRIES:
        out.append(Choice("tibber", "energy", "PT15M", token=True))
    if country == "SE":
        out.append(nordic_site)
    elif country in NORDIC:
        out += [_energy_charts(zone), nordic_site]
    elif country in IBERIA:
        out.append(Choice("omie", "spot", "PT15M"))
    elif all(z in CC_BY for z, a in ZONES.items() if a.country == country):
        out.append(_energy_charts(zone))
    elif entsoe:
        out += [entsoe_choice, _energy_charts(zone)]
    else:
        out.append(_energy_charts(zone))
    if entsoe and entsoe_choice not in out:
        out.append(entsoe_choice)
    return out
