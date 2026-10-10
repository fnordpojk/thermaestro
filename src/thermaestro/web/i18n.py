"""Languages and formats: one gettext catalog per language, read from its .po file at
start, and numbers and times formatted by Babel.

The language and the formats are chosen apart: a region, which with the language gives
CLDR's formats (English and Sweden: 2026-10-06, 23:30, 1 234,5), and optional overrides
for the date style, the clock and the decimal sign. Times are in the house's time zone.

Jinja2's gettext functions belong to the whole environment, so the language and formats
of the request being rendered sit in context variables.
"""

import gettext
import io
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, datetime, tzinfo
from functools import cache
from importlib import resources

from babel import Locale, UnknownLocaleError, negotiate_locale
from babel.dates import format_date, format_datetime, format_time
from babel.messages.mofile import write_mo
from babel.messages.pofile import read_po
from babel.numbers import format_decimal

from ..auth.preferences import Preferences

LANGUAGES = ("en", "sv", "de")
DEFAULT = "en"
NOTHING = "\N{EN DASH}"
"""Shown for a value that isn't there."""
NAMES = {"en": "English", "sv": "Svenska", "de": "Deutsch"}

current: ContextVar[str] = ContextVar("language", default=DEFAULT)


@dataclass(frozen=True)
class Formats:
    locale: Locale
    dates: str | None = None
    clock: str | None = None
    decimal: str | None = None
    zone: tzinfo = field(default=UTC)

    @property
    def tag(self) -> str:
        """The locale as browsers name it, such as en-SE."""
        return str(self.locale).replace("_", "-")


# Formats is frozen, so the default is never changed in place.
formats: ContextVar[Formats] = ContextVar(
    "formats",
    default=Formats(Locale.parse(DEFAULT)),  # noqa: B039
)


def format_locale(language: str, region: str | None) -> Locale:
    """The language in the region where CLDR has it (en_SE); else the region's main
    locale, whose short formats are only digits and signs (sv with GB: en_GB's)."""
    for code in (f"{language}_{region}", f"und_{region}") if region else ():
        try:
            return Locale.parse(code)
        except (UnknownLocaleError, ValueError):
            continue
    return Locale.parse(language)


@cache
def regions(language: str) -> list[tuple[str, str]]:
    """The countries to choose from, as (code, name in the language), by name."""
    names = Locale.parse(language).territories
    out = []
    for code, name in names.items():
        if len(code) == 2 and code.isalpha():
            try:
                Locale.parse(f"und_{code}")
            except (UnknownLocaleError, ValueError):
                continue
            out.append((code, name))
    return sorted(out, key=lambda pair: pair[1])


def weekday(day: int) -> str:
    """A day of the week by its number, 0 Monday, in the page's language."""
    return str(Locale.parse(current.get()).days["format"]["wide"][day]).capitalize()


def country(code: str) -> str:
    """A country's name in the language in use."""
    return str(Locale.parse(current.get()).territories.get(code, code))


def browser_region(accept_language: str | None) -> str | None:
    """The country of the browser's first language, as in en-SE."""
    first = (accept_language or "").split(",")[0].split(";")[0].strip()
    parts = first.replace("_", "-").split("-")
    if len(parts) >= 2 and len(parts[-1]) == 2 and parts[-1].isalpha():
        return parts[-1].upper()
    return None


def resolve(
    preferences: Preferences | None,
    cookie: str | None,
    accept_language: str | None,
    zone: tzinfo,
) -> tuple[str, Formats]:
    """The language and formats for a request: the user's choices, else the browser's."""
    p = preferences or Preferences()
    language = p.language or choose(cookie, accept_language)
    region = p.region or browser_region(accept_language)
    locale = format_locale(language, region)
    return language, Formats(locale, p.dates, p.clock, p.decimal, zone)


@contextmanager
def using(language: str, chosen: Formats) -> Iterator[None]:
    language_token = current.set(language)
    formats_token = formats.set(chosen)
    try:
        yield
    finally:
        formats.reset(formats_token)
        current.reset(language_token)


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
    # the MQTT client
    mark("off"),
    mark("connecting"),
    mark("connected"),
    mark("failed"),
    # Home Assistant discovery
    mark("publishing"),
    mark("sweeping"),
    # a device's own warning, under "Needs attention"
    mark("warning"),
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
    mark("make and revoke one's own wall displays, which stay logged in"),
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
        "that password is too easy to guess: it is a common one, a common one with"
        " digits or symbols around it, a repeat or a run, or contains the user name or"
        " the product's"
    ),
    mark("enter your password again to make this change"),
    mark("a display's name is 1 to 64 characters"),
    mark("a wall display can't make changes that need a password"),
    mark("this display link has been used, has expired, or was revoked"),
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
    f = formats.get()
    pattern = "#,##0" if digits == 0 else "#,##0." + "0" * digits
    text = format_decimal(value, format=pattern, locale=f.locale)
    symbols = f.locale.number_symbols["latn"]
    decimal, group = symbols["decimal"], symbols["group"]
    wanted = {"point": ".", "comma": ","}.get(f.decimal or "", decimal)
    if wanted != decimal:
        # The grouping sign stays unless it is now the decimal sign's twin.
        twin = "," if wanted == "." else "\N{NO-BREAK SPACE}"
        group_now = twin if group in (".", ",") else group
        text = text.translate({ord(decimal): wanted, ord(group): group_now})
    return text


def when(t: float | str | None) -> str:
    """A moment, in the house's time zone: seconds since the epoch, or ISO 8601 text."""
    if t is None:
        return NOTHING
    if isinstance(t, str):
        try:
            moment = datetime.fromisoformat(t)
        except ValueError:
            return t
    else:
        moment = datetime.fromtimestamp(t, UTC)
    f = formats.get()
    if not f.dates and not f.clock:
        return format_datetime(moment, format="short", tzinfo=f.zone, locale=f.locale)
    local = moment.astimezone(f.zone)
    date = format_date(local, "yyyy-MM-dd" if f.dates == "iso" else "short", locale=f.locale)
    clock = {"24": "HH:mm", "12": "h:mm a"}.get(f.clock or "", "short")
    return f"{date} {format_time(local, clock, locale=f.locale)}"


UNITS = {
    "degC": "\N{DEGREE SIGN}C",
    "g/m3": "g/m\N{SUPERSCRIPT THREE}",
    "m3": "m\N{SUPERSCRIPT THREE}",
    "W/m2": "W/m\N{SUPERSCRIPT TWO}",
    "deg": "\N{DEGREE SIGN}",
    "K": "\N{DEGREE SIGN}C",
}
"""Unit codes as written for people; the API keeps the codes. A difference (K) is shown as
°C, as the pump's own delta-T settings beside it are; the API and Home Assistant keep K."""


def unit(code: str | None) -> str:
    return "" if code is None else UNITS.get(code, code)


def decimal_symbol() -> str:
    f = formats.get()
    chosen = {"point": ".", "comma": ","}.get(f.decimal or "")
    return chosen or str(f.locale.number_symbols["latn"]["decimal"])


def zone_name() -> str:
    zone = formats.get().zone
    return str(getattr(zone, "key", "UTC"))
