"""What the household types, read as a request for a while: in English, Swedish and German."""

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from thermaestro.intents import Level
from thermaestro.intents.understand import understand

ZONE = ZoneInfo("Europe/Stockholm")
NOW = datetime(2026, 10, 10, 15, 0, tzinfo=ZONE)  # a Saturday afternoon
CS1, CS2, TANK = "pump:hp1/cs1", "pump:hp1/cs2", "pump:hp1/dhw"
SYSTEMS = [(CS1, "Climate system 1"), (CS2, "Upstairs")]
LEVELS = {
    "home": Level(id="home", name="Home", scope="house", low=20.5, high=22.0),
    "frost": Level(id="frost", name="Away", scope="house", low=15.0, high=17.0),
    "cosy": Level(id="cosy", name="Cosy", scope=CS1, low=21.5, high=23.0),
}


def at(day: int, hour: int, minute: int = 0) -> str:
    return datetime(2026, 10, day, hour, minute, tzinfo=ZONE).isoformat()


def read(text: str, **kw: Any) -> tuple[str | None, dict[str, Any], tuple[str, ...]]:
    found = understand(
        text,
        now=NOW,
        systems=kw.get("systems", SYSTEMS),
        tanks=[(TANK, "Hot water")],
        levels=kw.get("levels", LEVELS),
    )
    return found.kind, found.request, found.missing


@pytest.mark.parametrize(
    ("text", "offset", "until", "scope"),
    [
        ("a bit warmer until tonight", 1.0, at(10, 22), CS1),
        ("varmare 2 grader", 2.0, None, CS1),
        ("För varmt!", -1.0, None, CS1),
        ("kälter, zwei Grad, für 3 Stunden", -2.0, at(10, 18), CS1),
        ("half a degree cooler", -0.5, None, CS1),
        ("varmare i klimatsystem 2", 1.0, None, CS2),
        ("warmer upstairs till 21:30", 1.0, at(10, 21, 30), CS2),
        ("much warmer", 2.0, None, CS1),
        ("zu kalt bis morgen 7 Uhr", 1.0, at(11, 7), CS1),
        ("+1.5", None, None, None),
    ],
)
def test_warmer_and_cooler(
    text: str, offset: float | None, until: str | None, scope: str | None
) -> None:
    kind, request, missing = read(text)
    if offset is None:
        assert kind is None
        return
    assert kind == "warmer"
    assert request["offset"] == offset
    assert request.get("until") == until
    assert request["scope"] == scope
    assert missing == ()


@pytest.mark.parametrize(
    ("text", "by", "at_least"),
    [
        ("bad kl 19.30", at(10, 19, 30), 50.0),
        ("bath at 7pm, 55 degrees", at(10, 19), 55.0),
        ("Badewanne morgen früh", at(11, 7), 50.0),
        ("bad 19", at(10, 19), 50.0),
        ("varmvatten till 7", at(11, 7), 50.0),
        ("a bath tonight", at(10, 19), 50.0),
    ],
)
def test_a_bath_or_hot_water_by_a_time(text: str, by: str, at_least: float) -> None:
    kind, request, missing = read(text)
    assert (kind, request["by"], request["at_least"], request["scope"]) == (
        "bath",
        by,
        at_least,
        TANK,
    )
    assert missing == ()


def test_a_bath_without_a_time_asks_for_one() -> None:
    assert read("ett bad") == ("bath", {"kind": "bath", "scope": TANK, "at_least": 50.0}, ("by",))


@pytest.mark.parametrize("text", ["extra varmvatten", "more hot water", "boost", "mehr Warmwasser"])
def test_one_extra_charge(text: str) -> None:
    assert read(text) == ("boost_now", {"kind": "boost_now", "scope": TANK}, ())


@pytest.mark.parametrize(
    ("text", "until"),
    [
        ("borta till söndag", at(11, 18)),
        ("weg bis 12.10.", at(12, 18)),
        ("away until 20 oct at 16", at(20, 16)),
        ("bortresta till fredag kl 20", at(16, 20)),
        ("away for 3 days", at(13, 15)),
    ],
)
def test_away(text: str, until: str) -> None:
    kind, request, missing = read(text)
    assert (kind, request["until"]) == ("away", until)
    assert request["levels"] == {CS1: "frost", CS2: "frost"}  # the level named for it
    assert missing == ()


def test_away_without_a_time_or_levels() -> None:
    kind, request, missing = read("vi är borta", levels={})
    assert (kind, missing) == ("away", ("until", "levels"))
    assert request["levels"] == {}


def test_guests_get_the_warmest_level() -> None:
    kind, request, missing = read("Gäste bis morgen 18 Uhr")
    assert (kind, request["until"]) == ("guests", at(11, 18))
    assert request["levels"] == {CS1: "cosy", CS2: "home"}
    assert missing == ()


def test_hands_off_and_a_fireplace() -> None:
    assert read("hands off for 2 days")[1] == {"kind": "hands_off", "until": at(12, 15)}
    assert read("rör inte pumpen")[2] == ("until",)
    assert read("brasan är tänd") == ("fireplace", {"kind": "fireplace"}, ())
    assert read("Der Kamin brennt")[0] == "fireplace"


@pytest.mark.parametrize("text", ["vad blir det för väder", "hello", "", "Wie spät ist es?"])
def test_what_isnt_understood(text: str) -> None:
    assert read(text) == (None, {}, ())


EXAMPLES = {
    "a bit warmer until 22": "warmer",
    "a bath at 19:30": "bath",
    "extra hot water": "boost_now",
    "away until Sunday": "away",
    "guests until tomorrow 18:00": "guests",
    "the fireplace is lit": "fireplace",
    "hands off for 2 days": "hands_off",
}


@pytest.mark.parametrize("language", ["en", "sv", "de"])
def test_the_examples_shown_are_understood_in_every_language(language: str) -> None:
    """What the conversation suggests saying, as each language's catalog has it, is read as
    meant, and complete."""
    from thermaestro.web import i18n

    translate = i18n.translations(language).gettext
    for example, kind in EXAMPLES.items():
        found, _, missing = read(translate(example))
        assert (found, missing) == (kind, ()), (language, translate(example))
    placeholder = translate("Or say it: a bath at 19:30, away until Sunday, a bit warmer until 22")
    phrases = placeholder.split(":", 1)[1].split(",")
    assert [read(p)[0] for p in phrases] == ["bath", "away", "warmer"], (language, phrases)
