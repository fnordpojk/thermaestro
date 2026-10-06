from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from babel import Locale
from babel.messages.extract import extract_from_dir
from babel.messages.frontend import parse_mapping_cfg
from babel.messages.pofile import read_po

import thermaestro
from thermaestro.auth import Preferences
from thermaestro.web import i18n

ROOT = Path(__file__).parent.parent
PACKAGE = Path(thermaestro.__file__).parent
LOCALE = PACKAGE / "web" / "locale"


def _ids(path: Path) -> set[str]:
    with path.open("rb") as f:
        return {m.id for m in read_po(f) if m.id and isinstance(m.id, str)}


def test_the_template_has_every_message_in_the_code() -> None:
    with (ROOT / "babel.cfg").open() as f:
        method_map, options_map = parse_mapping_cfg(f)  # type: ignore[no-untyped-call]
    keywords = {"_": None, "gettext": None, "ngettext": (1, 2), "mark": None}
    found = {
        message
        for _, _, message, _, _ in extract_from_dir(
            PACKAGE, method_map, options_map, keywords=keywords
        )
        if isinstance(message, str)
    }
    template = _ids(LOCALE / "messages.pot")
    assert found - template == set(), "new messages: extract the catalog template again"
    assert template - found == set(), "messages no longer used: extract the template again"


@pytest.mark.parametrize("language", [lang for lang in i18n.LANGUAGES if lang != i18n.DEFAULT])
def test_every_message_is_translated(language: str) -> None:
    template = _ids(LOCALE / "messages.pot")
    with (LOCALE / language / "LC_MESSAGES" / "messages.po").open("rb") as f:
        catalog = read_po(f, locale=language)
    translated = {m.id for m in catalog if m.id and m.string}
    assert template - translated == set()
    assert not [m.id for m in catalog if m.fuzzy and m.id]


def test_languages_are_chosen_by_cookie_then_browser() -> None:
    assert i18n.choose("de", "sv-SE,sv;q=0.9") == "de"
    assert i18n.choose(None, "sv-SE,sv;q=0.9,en;q=0.5") == "sv"
    assert i18n.choose("xx", "fr-FR, de;q=0.5") == "de"
    assert i18n.choose(None, "fr-FR") == "en"
    assert i18n.choose(None, None) == "en"


def test_numbers_and_units() -> None:
    with i18n.using("sv", i18n.Formats(Locale.parse("sv"))):
        assert i18n.number(-4.25) in ("-4,2", "\N{MINUS SIGN}4,2")
        assert i18n.number(None) == i18n.NOTHING
        assert i18n.unit("degC") == "\N{DEGREE SIGN}C"
        assert i18n.unit("kWh") == "kWh"
        assert i18n._("Log in") == "Logga in"


MOMENT = datetime(2026, 10, 6, 21, 30, tzinfo=UTC).timestamp()
STOCKHOLM = ZoneInfo("Europe/Stockholm")


def shown(preferences: Preferences, accept: str | None = None) -> tuple[str, str, str]:
    language, chosen = i18n.resolve(preferences, None, accept, STOCKHOLM)
    with i18n.using(language, chosen):
        return language, i18n.when(MOMENT), i18n.number(-1234.5)


def test_english_with_swedish_formats() -> None:
    # The owner's case: English, with ISO dates and a 24-hour clock.
    assert shown(Preferences(language="en", region="SE")) == (
        "en",
        "2026-10-06, 23:30",
        "-1\N{NO-BREAK SPACE}234,5",
    )
    assert shown(Preferences(language="en", region="SE", decimal="point")) == (
        "en",
        "2026-10-06, 23:30",
        "-1\N{NO-BREAK SPACE}234.5",
    )


def test_overrides_on_the_language_alone() -> None:
    assert shown(Preferences(language="en")) == (
        "en",
        "10/6/26, 11:30\N{NARROW NO-BREAK SPACE}PM",
        "-1,234.5",
    )
    _, moment, _ = shown(Preferences(language="en", dates="iso", clock="24"))
    assert moment == "2026-10-06 23:30"
    _, _, comma = shown(Preferences(language="en", decimal="comma"))
    assert comma == "-1\N{NO-BREAK SPACE}234,5"
    _, _, point = shown(Preferences(language="de", decimal="point"))
    assert point == "-1,234.5"
    _, twelve, _ = shown(Preferences(language="sv", clock="12"))
    assert twelve.startswith("2026-10-06 11:30")


def test_a_region_cldr_lacks_for_the_language() -> None:
    # Swedish with British formats: the region's own short formats.
    language, moment, _ = shown(Preferences(language="sv", region="GB"))
    assert (language, moment) == ("sv", "06/10/2026, 23:30")


def test_the_browser_decides_what_the_user_left_open() -> None:
    assert shown(Preferences(), "en-SE,en;q=0.9")[:2] == ("en", "2026-10-06, 23:30")
    assert shown(Preferences(), "de-DE")[:2] == ("de", "06.10.26, 23:30")
    assert shown(Preferences(region="GB"), "sv-SE")[:2] == ("sv", "06/10/2026, 23:30")


def test_audit_times_are_shown_in_the_house_zone() -> None:
    _, chosen = i18n.resolve(Preferences(language="sv"), None, None, STOCKHOLM)
    with i18n.using("sv", chosen):
        assert i18n.when("2026-10-06T21:30:00.000+00:00") == "2026-10-06 23:30"
        assert i18n.when("not a time") == "not a time"


def test_regions_are_named_in_the_language() -> None:
    assert ("SE", "Sverige") in i18n.regions("sv")
    assert ("SE", "Sweden") in i18n.regions("en")
    assert all(len(code) == 2 for code, _ in i18n.regions("de"))
