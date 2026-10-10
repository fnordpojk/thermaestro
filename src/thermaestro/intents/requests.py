"""Asking for an intent in household terms: one request per kind, with only what that kind
takes, built into an intent. The web UI, the API and MQTT use the same requests."""

from collections.abc import Mapping
from datetime import datetime, time
from typing import Annotated, Any

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from . import kinds
from .model import Context, Day, Intent, Kind, MonthDay, Scope, Strength
from .resolve import RANKS


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Span(_Model):
    days: tuple[Day, ...] = ()
    """Days of the week, 0 Monday; none: every day."""
    start: time | None = None
    end: time | None = None
    """Local times; none: all day. An end before the start runs past midnight."""


class Step(Span):
    """One step of a weekly pattern: a level, on days and between times."""

    level: str


class Due(_Model):
    """Hot water by a time, on days of the week: a level's top, or a temperature."""

    by: time
    days: tuple[Day, ...] = ()
    level: str | None = None
    temp: float | None = None


class Request(_Model):
    """What to ask for. Each kind takes only its own fields:

    - `warmer`: `scope`, `offset` (°C, negative for cooler), `until` (else the next change)
    - `bath`: `scope` (the tank), `at_least`, `by`, `strength`
    - `guests`: `until`, `levels` (climate system to level), `hot_water` (a tank level)
    - `away`: `until`, `levels`
    - `hands_off`: `until`
    - `fireplace`, `boost_now`: `scope`
    - `comfort_band`: `scope`, `pattern`, `season`; or `no_sensor` with `steps`
    - `hot_water_by`: `scope`, `deadlines`, `strength`
    - `hot_water_floor`: `scope`, `temp`
    - `cost_stance`: `ranking`, `slider` (0 to 1)
    - `addition_policy`: `policy`, `kw`
    - `pool`: `scope`, `level`, `spans`
    - `power_peak`: `kw`, `spans`
    - `quiet_hours`: `spans`
    """

    kind: Kind
    scope: Scope = "house"
    offset: Annotated[float, Field(ge=-5, le=5)] | None = None
    until: AwareDatetime | None = None
    at_least: Annotated[float, Field(ge=20, le=90)] | None = None
    by: AwareDatetime | None = None
    strength: Strength | None = None
    levels: dict[str, str] = Field(default_factory=dict)
    hot_water: str | None = None
    pattern: tuple[Step, ...] = ()
    season: tuple[MonthDay, MonthDay] | None = None
    no_sensor: bool = False
    steps: Annotated[int, Field(ge=1, le=5)] | None = None
    deadlines: tuple[Due, ...] = ()
    temp: Annotated[float, Field(ge=20, le=90)] | None = None
    ranking: tuple[str, ...] | None = None
    slider: Annotated[float, Field(ge=0, le=1)] | None = None
    policy: kinds.Addition | None = None
    kw: Annotated[float, Field(ge=0, le=100)] | None = None
    level: str | None = None
    spans: tuple[Span, ...] = ()


ALLOWED: Mapping[str, frozenset[str]] = {
    "warmer": frozenset({"scope", "offset", "until"}),
    "bath": frozenset({"scope", "at_least", "by", "strength"}),
    "guests": frozenset({"until", "levels", "hot_water"}),
    "away": frozenset({"until", "levels"}),
    "hands_off": frozenset({"until"}),
    "fireplace": frozenset({"scope"}),
    "boost_now": frozenset({"scope"}),
    "comfort_band": frozenset({"scope", "pattern", "season", "no_sensor", "steps"}),
    "hot_water_by": frozenset({"scope", "deadlines", "strength"}),
    "hot_water_floor": frozenset({"scope", "temp"}),
    "cost_stance": frozenset({"ranking", "slider"}),
    "addition_policy": frozenset({"policy", "kw"}),
    "pool": frozenset({"scope", "level", "spans"}),
    "power_peak": frozenset({"kw", "spans"}),
    "quiet_hours": frozenset({"spans"}),
}
"""The fields each kind takes, besides `kind`."""


def _span(s: Span) -> kinds.Span:
    return (s.days, s.start, s.end)


def build(request: Request, *, principal: str, now: datetime) -> Intent:
    """The intent a request asks for. ValueError, saying what is missing or extra."""
    given = {k for k in request.model_fields_set if k != "kind"}
    extra = given - ALLOWED[request.kind]
    if extra:
        raise ValueError(f"{request.kind} doesn't take {', '.join(sorted(extra))}")
    r = request
    who: dict[str, Any] = {"principal": principal, "created": now}

    def need(*names: str) -> None:
        missing = [n for n in names if getattr(r, n) in (None, (), {})]
        if missing:
            raise ValueError(f"{r.kind} needs {', '.join(missing)}")

    match r.kind:
        case "warmer":
            need("offset")
            assert r.offset is not None  # noqa: S101 - checked
            return kinds.warmer(r.scope, r.offset, until=r.until, **who)
        case "bath":
            need("at_least", "by")
            assert r.at_least is not None  # noqa: S101 - checked
            assert r.by is not None  # noqa: S101
            return kinds.bath(r.scope, r.at_least, r.by, strength=r.strength or "must", **who)
        case "guests":
            need("until", "levels")
            assert r.until is not None  # noqa: S101
            return kinds.guests(r.until, r.levels, hot_water=r.hot_water, **who)
        case "away":
            need("until", "levels")
            assert r.until is not None  # noqa: S101
            return kinds.away(r.until, r.levels, **who)
        case "hands_off":
            need("until")
            assert r.until is not None  # noqa: S101
            return kinds.hands_off(r.until, **who)
        case "fireplace":
            return kinds.fireplace(r.scope, **who)
        case "boost_now":
            return kinds.boost_now(r.scope, **who)
        case "comfort_band":
            if r.no_sensor:
                return kinds.no_sensor_band(r.scope, steps=r.steps or kinds.SHIFT_STEPS, **who)
            need("pattern")
            steps = [(s.level, _span(s)) for s in r.pattern]
            return kinds.comfort_band(r.scope, steps, season=r.season, **who)
        case "hot_water_by":
            need("deadlines")
            due: list[tuple[str | float, tuple[int, ...], time]] = []
            for d in r.deadlines:
                if (d.level is None) == (d.temp is None):
                    raise ValueError("a deadline names a level or a temperature")
                due.append((d.level if d.level is not None else float(d.temp or 0), d.days, d.by))
            return kinds.hot_water_by(r.scope, due, strength=r.strength or "should", **who)
        case "hot_water_floor":
            need("temp")
            assert r.temp is not None  # noqa: S101
            return kinds.hot_water_floor(r.scope, r.temp, **who)
        case "cost_stance":
            need("slider")
            assert r.slider is not None  # noqa: S101
            ranking = r.ranking or RANKS
            if sorted(ranking) != sorted(RANKS):
                raise ValueError(f"the ranking orders these, each once: {', '.join(RANKS)}")
            return kinds.cost_stance(ranking=ranking, slider=r.slider, **who)
        case "addition_policy":
            need("policy")
            assert r.policy is not None  # noqa: S101
            if r.policy == "limit":
                need("kw")
            return kinds.addition_policy(r.policy, kw=r.kw, **who)
        case "pool":
            need("level")
            assert r.level is not None  # noqa: S101
            pattern = [_span(s) for s in r.spans] or [((), None, None)]
            return kinds.pool(r.scope, r.level, pattern=pattern, **who)
        case "power_peak":
            need("kw")
            assert r.kw is not None  # noqa: S101
            return kinds.power_peak(r.kw, pattern=[_span(s) for s in r.spans], **who)
        case "quiet_hours":
            need("spans")
            return kinds.quiet_hours([_span(s) for s in r.spans], **who)
    raise ValueError(f"no such kind: {r.kind}")  # pragma: no cover - Kind is closed


def request_of(intent: Intent) -> Request:
    """The request that asks for an intent as it is: what changing it starts from.
    Building it again (at the intent's own time) gives the same intent."""
    p, e, v = intent.parameters, intent.expectations, intent.validity
    first = e[0].targets[0] if e else None

    def spans() -> tuple[Span, ...]:
        return tuple(Span(days=c.days, start=c.start, end=c.end) for x in e for c in x.contexts)

    def number(value: object) -> float | None:
        return float(value) if isinstance(value, int | float) else None

    k, scope = intent.kind, intent.scope
    match k:
        case "warmer":
            return Request(
                kind=k,
                scope=scope,
                offset=number(p.get("offset")),
                until=v.at if v.ends == "at" else None,
            )
        case "bath":
            assert first is not None  # noqa: S101 - a bath has its target
            return Request(
                kind=k,
                scope=scope,
                at_least=number(first.value),
                by=e[0].contexts[0].at,
                strength=intent.strength,
            )
        case "guests" | "away":
            levels = p.get("levels")
            chosen = {str(a): str(b) for a, b in levels.items()} if isinstance(levels, dict) else {}
            if k == "away":
                return Request(kind=k, until=v.at, levels=chosen)
            hot_water = p.get("hot_water")
            return Request(
                kind=k,
                until=v.at,
                levels=chosen,
                hot_water=hot_water if isinstance(hot_water, str) else None,
            )
        case "hands_off":
            return Request(kind=k, until=v.at)
        case "fireplace" | "boost_now":
            return Request(kind=k, scope=scope)
        case "comfort_band":
            if p.get("no_sensor"):
                steps = int(first.high) if first is not None and first.high is not None else None
                return Request(kind=k, scope=scope, no_sensor=True, steps=steps)
            pattern = tuple(
                Step(level=x.targets[0].level or "", days=c.days, start=c.start, end=c.end)
                for x in e
                for c in x.contexts or (Context(),)
            )
            season = next((c.season for x in e for c in x.contexts if c.season), None)
            return Request(kind=k, scope=scope, pattern=pattern, season=season)
        case "hot_water_by":
            deadlines = tuple(
                Due(
                    by=c.by or time(),
                    days=c.days,
                    level=x.targets[0].level,
                    temp=None if x.targets[0].level else number(x.targets[0].value),
                )
                for x in e
                for c in x.contexts
            )
            return Request(kind=k, scope=scope, deadlines=deadlines, strength=intent.strength)
        case "hot_water_floor":
            return Request(kind=k, scope=scope, temp=number(first.value if first else None))
        case "cost_stance":
            ranking = p.get("ranking")
            return Request(
                kind=k,
                ranking=tuple(str(r) for r in ranking) if isinstance(ranking, list) else None,
                slider=number(p.get("slider")),
            )
        case "addition_policy":
            policy = first.value if first is not None else None
            return Request(
                kind=k,
                policy=policy if isinstance(policy, str) else None,
                kw=number(p.get("kw")),
            )
        case "pool":
            return Request(kind=k, scope=scope, level=first.level if first else None, spans=spans())
        case "power_peak":
            return Request(kind=k, kw=number(first.value if first else None), spans=spans())
        case "quiet_hours":
            return Request(kind=k, spans=spans())
    raise ValueError(f"no such kind: {k}")  # pragma: no cover - Kind is closed
