from pathlib import Path

import pytest
from babel.messages.extract import extract_from_dir
from babel.messages.frontend import parse_mapping_cfg
from babel.messages.pofile import read_po

import thermaestro
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
    token = i18n.current.set("sv")
    try:
        assert i18n.number(-4.25) in ("-4,2", "\N{MINUS SIGN}4,2")
        assert i18n.number(None) == i18n.NOTHING
        assert i18n.unit("degC") == "\N{DEGREE SIGN}C"
        assert i18n.unit("kWh") == "kWh"
        assert i18n._("Log in") == "Logga in"
    finally:
        i18n.current.reset(token)
