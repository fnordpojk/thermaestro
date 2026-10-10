"""What the household types, read as a request for a while: "a bit warmer until tonight",
"bad kl 19.30", "borta till söndag", "Gäste bis morgen 18 Uhr".

Built-in rules, in English, Swedish and German; nothing leaves the house. It reads the
request kinds for a while (warmer or cooler, a bath or hot water by a time, one extra
charge, away, guests, a fireplace, hands off), a temperature step, and when: a clock time,
a day (today, tomorrow, a weekday, a date), a part of the day, or a duration. What it can't
tell is left for the household to fill in: the answer is shown as a form to check before
anything is asked.
"""

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Any

from .model import Level

BATH_C = 50.0
"""A bath's water, or hot water by a time, when no temperature is said."""
UNTIL_DAY = time(18, 0)
"""The time of an end given as a day only ("away until Sunday")."""
BY_DAY = time(19, 0)
"""The time of a deadline given as a day only."""
EVENING_UNTIL, EVENING_BY = time(22, 0), time(19, 0)
MORNING = time(7, 0)

Ref = tuple[str, str]
"""A node's reference and its name: `("pump:hp1/cs1", "Climate system 1")`."""


@dataclass(frozen=True)
class Understood:
    kind: str | None
    """None: not understood."""
    request: dict[str, Any] = field(default_factory=dict)
    """The request's fields, as far as they could be read; times are ISO, in the house's
    zone."""
    missing: tuple[str, ...] = ()
    """What the request still needs: `until`, `by`, `levels`."""


# --- what is asked for ----------------------------------------------------------------------

# The first that matches decides; complaints come before the words inside them.
KINDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("hands_off", ("hands off", "don't touch", "do not touch", "leave it alone",
                   "händerna borta", "rör inte", "låt bli", "finger weg", "hände weg",
                   "nicht anfassen")),
    ("cooler", ("too warm", "too hot", "för varmt", "för varm", "zu warm", "zu heiß")),
    ("warmer", ("too cold", "too cool", "freezing", "för kallt", "för kall", "fryser",
                "zu kalt", "zu kühl", "friere", "frieren")),
    ("away", ("away", "borta", "bortrest", "bortresta", "på resa", "abwesend", "verreist",
              "weg", "nicht zu hause", "nicht da", "vacation", "holiday", "semester",
              "urlaub")),
    ("guests", ("guests", "guest", "visitors", "gäster", "gäst", "besök",
                "gäste", "gast", "besuch")),
    ("bath", ("bath", "bathe", "bad", "badet", "bada", "baden", "badewanne")),
    ("hot_water", ("hot water", "more hot water", "extra hot water", "varmvatten",
                   "mer varmvatten", "extra varmvatten", "warmwasser", "mehr warmwasser",
                   "extra warmwasser", "boost", "extra charge")),
    ("fireplace", ("fireplace", "fire", "wood stove", "stove", "brasa", "brasan",
                   "braskamin", "kamin", "kaminen", "eldar", "eldat", "feuer", "ofen",
                   "kachelofen", "kaminfeuer")),
    ("cooler", ("cooler", "colder", "cool", "svalare", "kallare", "svalt", "kühler",
                "kälter")),
    ("warmer", ("warmer", "hotter", "warm", "heat", "varmare", "varmt", "värme", "wärmer",
                "heizen", "wärmer machen")),
)  # fmt: skip

WORD_NUMBERS = {
    "one": 1, "a": 1, "an": 1, "en": 1, "ett": 1, "ein": 1, "eine": 1, "einen": 1,
    "two": 2, "två": 2, "zwei": 2, "three": 3, "tre": 3, "drei": 3,
    "four": 4, "fyra": 4, "vier": 4, "five": 5, "fem": 5, "fünf": 5,
    "six": 6, "sex": 6, "sechs": 6, "half": 0.5, "halv": 0.5, "halb": 0.5, "halbe": 0.5,
    "halbes": 0.5,
}  # fmt: skip
NUMBER = r"(\d+(?:[.,]\d+)?|" + "|".join(sorted(WORD_NUMBERS, key=len, reverse=True)) + r")"
DEGREES = re.compile(
    rf"(?<![\w:.])([+-])?\s*{NUMBER}\s*(?:°\s*c?|degrees?|deg|grader|grad|graden|k)(?!\w)"
)
HALF_DEGREE = re.compile(r"\b(half a degree|en halv grad|ein halbes grad|halbes grad|halb grad)\b")
MUCH = re.compile(r"\b(much|a lot|lots|mycket|rejält|viel|deutlich)\b")

# --- when -----------------------------------------------------------------------------------

UNITS = {
    "minutes": 60, "minute": 60, "mins": 60, "min": 60, "minuter": 60, "minut": 60,
    "minuten": 60,
    "hours": 3600, "hour": 3600, "hrs": 3600, "hr": 3600, "h": 3600, "timmar": 3600,
    "timme": 3600, "tim": 3600, "t": 3600, "stunden": 3600, "stunde": 3600, "std": 3600,
    "days": 86400, "day": 86400, "dagar": 86400, "dag": 86400, "dygn": 86400,
    "tage": 86400, "tagen": 86400, "tag": 86400,
}  # fmt: skip
DURATION = re.compile(
    rf"\b(?:(?:for|in|i|om|för|für|in)\s+)?{NUMBER}\s*"
    rf"({'|'.join(sorted(UNITS, key=len, reverse=True))})\b"
)
HALF_HOUR = re.compile(r"\b(half an hour|en halvtimme|halvtimme|eine halbe stunde|halbe stunde)\b")
WEEKDAYS = {
    "monday": 0, "måndag": 0, "montag": 0, "tuesday": 1, "tisdag": 1, "dienstag": 1,
    "wednesday": 2, "onsdag": 2, "mittwoch": 2, "thursday": 3, "torsdag": 3,
    "donnerstag": 3, "friday": 4, "fredag": 4, "freitag": 4, "saturday": 5, "lördag": 5,
    "samstag": 5, "sonnabend": 5, "sunday": 6, "söndag": 6, "sonntag": 6,
}  # fmt: skip
MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "mär": 3, "apr": 4, "may": 5, "maj": 5, "mai": 5,
    "jun": 6, "jul": 7, "aug": 8, "sep": 9, "oct": 10, "okt": 10, "nov": 11, "dec": 12,
    "dez": 12,
}  # fmt: skip
DAY_AFTER = re.compile(r"\b(day after tomorrow|i övermorgon|övermorgon|übermorgen)\b")
TOMORROW = re.compile(r"(?<!am )\b(tomorrow|i morgon|imorgon|morgen)\b")
TODAY = re.compile(r"\b(today|idag|i dag|heute)\b")
EVENING = re.compile(
    r"\b(tonight|this evening|evening|ikväll|i kväll|kväll|kvällen|abend|abends)\b"
)
MORNING_WORDS = re.compile(r"\b(morning|i morse|morgon|morgonen|am morgen|früh|morgens)\b")
WEEKDAY = re.compile(r"\b(" + "|".join(WEEKDAYS) + r")(?:s|en)?\b")
BARE_HOUR = re.compile(r"(?<![\w.:,+-])([01]?\d|2[0-3])(?![\w.:,])")
BARE_STEP = re.compile(r"(?<![\w.:,])([+-]?[1-5](?:[.,]5)?)(?![\w.:,])")
ISO_DATE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
NUMERIC_DATE = re.compile(r"\b(\d{1,2})(?:/(\d{1,2})|\.(\d{1,2})\.)")
NAMED_DATE = re.compile(
    r"\b(?:(\d{1,2})\.?\s*("
    + "|".join(MONTHS)
    + r")[a-zä]*|("
    + "|".join(MONTHS)
    + r")[a-zä]*\.?\s*(\d{1,2}))\b"
)
CLOCK = re.compile(r"\b([01]?\d|2[0-3])[:.]([0-5]\d)\b(?:\s*(am|pm))?")
MARKED_HOUR = re.compile(
    r"\b(?:(?:kl|klockan|at|um|till|tills|until|by|bis|to|innan|before|vor)\.?\s+)"
    r"([01]?\d|2[0-3])\b(?:\s*(am|pm|uhr))?|\b([01]?\d|2[0-3])\s*(am|pm|uhr)\b"
)
SYSTEM = re.compile(r"\b(?:climate system|system|klimatsystem|krets|heizkreis|kreis|cs)\s*(\d)\b")

LEVEL_WORDS = {
    "away": ("away", "borta", "abwesend", "frost", "semester", "urlaub", "vacation", "low"),
    "guests": ("guests", "gäster", "gäste", "besuch", "komfort", "comfort"),
}


def understand(
    text: str,
    *,
    now: datetime,
    systems: Sequence[Ref],
    tanks: Sequence[Ref],
    levels: Mapping[str, Level],
) -> Understood:
    """`text` read as a request for a while; `now` in the house's time zone."""
    said = " " + re.sub(r"\s+", " ", text.lower().replace("\u2019", "'")).strip() + " "
    kind = _kind(said)
    if kind is None:
        return Understood(None)
    when, rest = _when(said, now)
    needs_time = kind in ("bath", "hot_water", "away", "guests", "hands_off")
    # "bad 19": a lone hour, where a time is what is needed.
    if when is None and needs_time and (m := BARE_HOUR.search(DEGREES.sub(" ", rest))):
        when = _When(clock=time(int(m.group(1)), 0))
    request: dict[str, Any] = {}
    missing: list[str] = []
    if kind in ("warmer", "cooler"):
        request = {"kind": "warmer", "scope": _system(said, systems)}
        step = _step(_without_systems(rest))
        request["offset"] = -step if kind == "cooler" else step
        if when is not None:
            request["until"] = _resolve(when, now, "until").isoformat()
    elif kind in ("bath", "hot_water") and (kind == "bath" or when is not None):
        request = {"kind": "bath", "scope": _first(tanks), "at_least": _hot(rest)}
        if when is None:
            missing.append("by")
        else:
            request["by"] = _resolve(when, now, "by").isoformat()
    elif kind == "hot_water":
        request = {"kind": "boost_now", "scope": _first(tanks)}
    elif kind == "fireplace":
        request = {"kind": "fireplace"}
    elif kind in ("away", "guests", "hands_off"):
        request = {"kind": kind}
        if when is None:
            missing.append("until")
        else:
            request["until"] = _resolve(when, now, "until").isoformat()
        if kind != "hands_off":
            chosen = {ref: _level(kind, ref, levels) for ref, _ in systems}
            request["levels"] = {ref: lv for ref, lv in chosen.items() if lv is not None}
            if len(request["levels"]) < len(systems) or not systems:
                missing.append("levels")
    return Understood(request["kind"], request, tuple(missing))


def _kind(said: str) -> str | None:
    """The first kind one of whose words is said, as a whole word."""
    for kind, words in KINDS:
        for word in words:
            if re.search(rf"(?<!\w){re.escape(word)}(?!\w)", said):
                return kind
    return None


def _first(refs: Sequence[Ref]) -> str:
    return refs[0][0] if refs else "house"


def _system(said: str, systems: Sequence[Ref]) -> str:
    """The climate system named, by its name or its number; else the first."""
    for ref, label in systems:
        if label and label.lower() in said:
            return ref
    found = SYSTEM.search(said)
    if found:
        for ref, _ in systems:
            if ref.endswith(f"cs{found.group(1)}"):
                return ref
    return _first(systems)


def _without_systems(said: str) -> str:
    """The text without a climate system's number, which isn't a step."""
    return SYSTEM.sub(" ", said)


def _number(word: str) -> float:
    return WORD_NUMBERS[word] if word in WORD_NUMBERS else float(word.replace(",", "."))


def _step(said: str) -> float:
    """How many degrees warmer or cooler: as said ("2 grader", "+1", "varmare 2"), else 2
    for "much", else 1."""
    if HALF_DEGREE.search(said):
        return 0.5
    found = DEGREES.search(said)
    if found:
        return max(0.5, min(5.0, abs(_number(found.group(2)))))
    bare = BARE_STEP.search(said)
    if bare:
        return max(0.5, min(5.0, abs(_number(bare.group(1)))))
    return 2.0 if MUCH.search(said) else 1.0


def _hot(said: str) -> float:
    """A bath's temperature, where one is said (30 °C or more), else BATH_C."""
    for found in DEGREES.finditer(said):
        value = _number(found.group(2))
        if 30 <= value <= 90:
            return value
    return BATH_C


@dataclass(frozen=True)
class _When:
    day: date | None = None
    clock: time | None = None
    part: str | None = None
    """"evening" or "morning", where no clock time is said."""
    after: timedelta | None = None


def _when(said: str, now: datetime) -> tuple[_When | None, str]:
    """When it ends or must be ready, if said; and the text without it, for the rest.
    Times are looked for with the temperatures blanked out, so 1.50 degrees isn't 01:50."""
    rest = said
    look = DEGREES.sub(" ", said)

    def take(found: re.Match[str]) -> None:
        nonlocal rest, look
        rest, look = rest.replace(found.group(0), " "), look.replace(found.group(0), " ")

    after = None
    if (m := HALF_HOUR.search(look)) is not None:
        after = timedelta(minutes=30)
        take(m)
    elif (m := DURATION.search(look)) is not None:
        after = timedelta(seconds=_number(m.group(1)) * UNITS[m.group(2)])
        take(m)
    day = None
    for pattern in (ISO_DATE, NAMED_DATE, NUMERIC_DATE):
        if (m := pattern.search(look)) is not None:
            day = _date(m, pattern, now)
            if day is not None:
                take(m)
                break
    if day is None:
        if (m := DAY_AFTER.search(look)) is not None:
            day = now.date() + timedelta(days=2)
            take(m)
        elif (m := TOMORROW.search(look)) is not None:
            day = now.date() + timedelta(days=1)
            take(m)
        elif (m := TODAY.search(look)) is not None:
            day = now.date()
            take(m)
        elif (m := WEEKDAY.search(look)) is not None:
            day = now.date() + timedelta(days=(WEEKDAYS[m.group(1)] - now.weekday() - 1) % 7 + 1)
            take(m)
    clock = None
    if (m := CLOCK.search(look)) is not None:
        clock = _clock(int(m.group(1)), int(m.group(2)), m.group(3))
        take(m)
    elif (m := MARKED_HOUR.search(look)) is not None:
        hour, suffix = (m.group(1), m.group(2)) if m.group(1) else (m.group(3), m.group(4))
        clock = _clock(int(hour), 0, suffix)
        take(m)
    part = None
    if clock is None:
        if EVENING.search(look):
            part = "evening"
        elif MORNING_WORDS.search(look):
            part = "morning"
    if day is None and clock is None and part is None and after is None:
        return None, rest
    return _When(day, clock, part, after), rest


def _clock(hour: int, minute: int, suffix: str | None) -> time | None:
    if suffix == "pm" and hour < 12:
        hour += 12
    elif suffix == "am" and hour == 12:
        hour = 0
    return time(hour % 24, minute)


def _date(m: re.Match[str], pattern: re.Pattern[str], now: datetime) -> date | None:
    try:
        if pattern is ISO_DATE:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        if pattern is NAMED_DATE:
            day = int(m.group(1) or m.group(4))
            month = MONTHS[(m.group(2) or m.group(3))[:3]]
        else:
            day, month = int(m.group(1)), int(m.group(2) or m.group(3))
        found = date(now.year, month, day)
    except (ValueError, KeyError):
        return None
    return found if found >= now.date() else found.replace(year=now.year + 1)


def _resolve(when: _When, now: datetime, purpose: str) -> datetime:
    """The moment: a duration from now; else a day and a time, today's or the next."""
    if when.after is not None and when.day is None and when.clock is None:
        return (now + when.after).replace(second=0, microsecond=0)
    clock = when.clock
    if clock is None:
        if when.part == "evening":
            clock = EVENING_UNTIL if purpose == "until" else EVENING_BY
        elif when.part == "morning":
            clock = MORNING
        else:
            clock = UNTIL_DAY if purpose == "until" else BY_DAY
    moment = datetime.combine(when.day or now.date(), clock, tzinfo=now.tzinfo)
    if when.day is None and moment <= now:
        moment += timedelta(days=1)
    return moment


def _level(kind: str, system: str, levels: Mapping[str, Level]) -> str | None:
    """The level for a climate system while away or with guests: one named for it, else
    the lowest band away and the highest with guests."""
    bands = [lv for lv in levels.values() if lv.low is not None and lv.scope in (system, "house")]
    if not bands:
        return None
    for lv in bands:
        name = f"{lv.id} {lv.name}".lower()
        if any(word in name for word in LEVEL_WORDS[kind]):
            return lv.id
    if kind == "away":
        return min(bands, key=lambda lv: lv.low or 0.0).id
    return max(bands, key=lambda lv: lv.high if lv.high is not None else lv.low or 0.0).id
