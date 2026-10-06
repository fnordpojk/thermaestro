"""Languages: one gettext catalog per language, read from its .po file at start, and
numbers and times formatted by Babel for the request's language.

Jinja2's gettext functions belong to the whole environment, so the language of the
request being rendered sits in a context variable.
"""

import gettext
import io
from contextvars import ContextVar
from datetime import UTC, datetime, tzinfo
from functools import cache
from importlib import resources

from babel import Locale, negotiate_locale
from babel.dates import format_datetime
from babel.messages.mofile import write_mo
from babel.messages.pofile import read_po
from babel.numbers import format_decimal

LANGUAGES = ("en", "sv", "de")
DEFAULT = "en"
NOTHING = "\N{EN DASH}"
"""Shown for a value that isn't there."""
NAMES = {"en": "English", "sv": "Svenska", "de": "Deutsch"}

current: ContextVar[str] = ContextVar("language", default=DEFAULT)


def mark(message: str) -> str:
    """Marks a message for the catalog where the code shows it by value."""
    return message


SHOWN_BY_VALUE = (
    # plugin instance states
    mark("starting"),
    mark("up"),
    mark("restarting"),
    mark("waiting"),
    mark("stopped"),
    # transport health
    mark("down"),
    mark("contended"),
    # value qualities
    mark("good"),
    mark("stale"),
    mark("not_connected"),
    mark("no_flow"),
    mark("transitional"),
    mark("assumed"),
    mark("out_of_range"),
    mark("unknown"),
    # what the rights allow
    mark("see devices, values and their history"),
    mark("see the installation's settings"),
    mark("change the installation's settings"),
    mark("enter or replace secrets (never read them back)"),
    mark("add, change and remove plugin instances"),
    mark("add, change and remove users and their rights"),
    mark("create and revoke one's own API tokens"),
    mark("read the audit log"),
    # refusals the pages show
    mark("wrong user name or password"),
    mark("the two passwords differ"),
    mark("the setup code isn't right, or has expired"),
    mark("there is already an administrator"),
    mark("too many attempts; wait a few minutes"),
    mark("the password you entered isn't right"),
    mark("that would leave no user who can manage users"),
    mark("a user name is 1 to 64 letters, digits or . _ @ -"),
    mark("a password needs at least 15 characters"),
    mark("a password can have at most 256 characters"),
    mark(
        "that password is too easy to guess: it is a common one, or contains the user"
        " name or the product's"
    ),
    mark("enter your password again to make this change"),
)


@cache
def translations(language: str) -> gettext.NullTranslations:
    if language == DEFAULT:
        return gettext.NullTranslations()
    po = resources.files(__package__).joinpath("locale", language, "LC_MESSAGES", "messages.po")
    with po.open("rb") as f:
        catalog = read_po(f, locale=language)
    buffer = io.BytesIO()
    write_mo(buffer, catalog)
    buffer.seek(0)
    return gettext.GNUTranslations(buffer)


def _(message: str, **values: object) -> str:
    text = translations(current.get()).gettext(message)
    return text % values if values else text


def ngettext(singular: str, plural: str, n: int, **values: object) -> str:
    text = translations(current.get()).ngettext(singular, plural, n)
    return text % {"num": n, **values}


def choose(cookie: str | None, accept_language: str | None) -> str:
    """The language a cookie asks for, else the best of the browser's, else English."""
    if cookie in LANGUAGES:
        return cookie
    wanted = []
    for part in (accept_language or "").split(","):
        tag = part.split(";")[0].strip()
        if tag:
            wanted.append(tag.replace("-", "_"))
    return negotiate_locale(wanted, LANGUAGES, sep="_") or DEFAULT


def number(value: float | None, digits: int = 1) -> str:
    if value is None:
        return NOTHING
    pattern = "#,##0" if digits == 0 else "#,##0." + "0" * digits
    return format_decimal(value, format=pattern, locale=Locale.parse(current.get()))


def when(t: float | None, zone: tzinfo = UTC) -> str:
    if t is None:
        return NOTHING
    moment = datetime.fromtimestamp(t, UTC)
    return format_datetime(moment, format="short", tzinfo=zone, locale=Locale.parse(current.get()))


UNITS = {
    "degC": "\N{DEGREE SIGN}C",
    "g/m3": "g/m\N{SUPERSCRIPT THREE}",
    "m3": "m\N{SUPERSCRIPT THREE}",
}
"""Unit codes as written for people; the API keeps the codes."""


def unit(code: str | None) -> str:
    return "" if code is None else UNITS.get(code, code)


def decimal_symbol() -> str:
    return str(Locale.parse(current.get()).number_symbols["latn"]["decimal"])
