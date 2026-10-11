"""What Thermaestro has learned, and which inputs work here: the operations behind the
page and the API. Seeing them needs `settings.read`; checking the inputs again, starting a
value over or pinning one needs `settings.write`."""

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from .. import clock, durations
from ..auth import AccountError
from ..core.executor import split
from ..learn import Learned, NotLearned, mark
from ..learn.hotwater import DEMAND
from ..learn.offers import DISMISSED_S, Offer, offers
from ..learn.tank import Demand
from . import i18n

if TYPE_CHECKING:
    from ..core.audit import AuditLog
    from ..core.values import Values
    from ..intents import Intents
    from ..learn import Check, Learning
    from .operations import Caller

NAMES = {
    "tank.charge_s": mark("How long a hot-water charge takes"),
    "tank.periodic": mark("The periodic hot-water increase, last seen"),
    "tank.loss": mark("The tank's standing loss"),
    "tank.demand": mark("Hot water drawn, through the week"),
    "tank.ceiling": mark("The warmest a charge has made the top"),
    "house.model": mark("How the rooms follow the heating and the weather"),
    "house.tau_h": mark("How fast the house loses its heat (its time constant)"),
    "house.response_h": mark("How long the heating takes to show in the rooms"),
    "pump.response": mark("How the supply follows the weather and the offset"),
    "compressor.draw": mark("What the compressor draws while it runs"),
    "compressor.starts": mark("How often the compressor starts"),
}
PINNABLE = frozenset({"tank.loss", "house.tau_h", "house.response_h"})
"""Models one number says, so the household may set it instead."""
MODEL_SHOWN = mark("off by %(error)s °C on average six hours ahead")
HOURS_SHOWN = mark("%(hours)s hours")
PUMP_SHOWN = mark("%(step)s °C of supply per offset step, %(slope)s °C per degree colder")
DRAW_SHOWN = {
    "heating": mark("%(kw)s kW heating"),
    "dhw": mark("%(kw)s kW making hot water"),
    "pool": mark("%(kw)s kW heating the pool"),
}
STARTS_SHOWN = mark("%(starts)s starts a day")
"""What each learned value is, in words."""
VERDICTS = {
    "usable": mark("Usable"),
    "not_usable": mark("Not usable on this pump"),
    "not_yet": mark("Not seen enough yet"),
}
STATES = {
    "starting": mark("Not learned yet"),
    "learning": mark("Learning"),
    "proved": mark("Proved"),
    "frozen": mark("Frozen"),
    "rejected": mark("Rejected"),
}
OFFER = mark("Hot water at %(what)s by %(by)s on %(days)s")
LOSS_SHOWN = mark("%(fall)s °C an hour at 50 °C")
WEEK_SHOWN = mark("%(total)s °C of the tank a week")
RECENT_DAYS = 7
SHADES = 4


class LearnOperations:
    """Mixed into the services, whose parts it uses."""

    if TYPE_CHECKING:
        learning: Learning | None
        intents: Intents | None
        values: Values
        audit: AuditLog

        def label(self, instance: str, path: str) -> str: ...

        async def ask(self, caller: Caller, body: dict[str, Any]) -> dict[str, Any]: ...

        def node_label(
            self, caller: Caller, instance: str, node: str, *, built_in: bool = False
        ) -> str: ...

    def _learning(self) -> "Learning":
        if self.learning is None:
            raise AccountError("learning isn't running here")
        return self.learning

    async def learned(self, caller: "Caller") -> dict[str, Any]:
        """Which inputs work here, what has been learned, and each tank's last week."""
        caller.principal.require("settings.read")
        learning = self._learning()
        checks = sorted(learning.checks.values(), key=lambda c: (c.instance, c.point))
        values = [self._value(caller, x) for x in learning.learnings.all()]
        since = clock.time() - RECENT_DAYS * 86_400
        tanks = []
        for keys in learning.hot_water.tanks():
            days = await learning.hot_water.events(keys.scope, "day", since)
            if not days:
                continue
            instance, path = split(keys.scope)
            tanks.append(
                {
                    "scope": keys.scope,
                    "part": self.node_label(caller, instance, path),
                    "days": len(days),
                    "draws": sum(int(b["draws"]) for _, b in days),
                    "drawn": round(sum(float(b["drawn"]) for _, b in days), 1),
                    "charges": sum(int(b["charges"]) for _, b in days),
                }
            )
        levels: dict[str, Any] = await self.intents.levels() if self.intents is not None else {}
        return {
            "inputs": [self._input(caller, c) for c in checks],
            "values": values,
            "tanks": tanks,
            "offers": [self._offer(caller, o, levels) for o in await self.offers(caller)],
        }

    def _offer(self, caller: "Caller", o: Offer, levels: dict[str, Any]) -> dict[str, Any]:
        instance, path = split(o.scope)
        level = levels.get(o.level) if o.level is not None else None
        days = ", ".join(i18n.weekday(d) for d in o.days)
        what = level.name if level is not None else f"{i18n.number(o.temp or 0.0, 0)} °C"
        return {
            "id": o.id,
            "scope": o.scope,
            "part": self.node_label(caller, instance, path),
            "by": f"{o.by:%H:%M}",
            "days": list(o.days),
            "drawn": o.drawn,
            "request": o.request(),
            "text": i18n._(OFFER, what=what, by=i18n.time_of_day(o.by), days=days),
        }

    async def offers(self, caller: "Caller") -> list[Offer]:
        """What the tanks' proved weeks suggest, where no deadline covers it yet."""
        caller.principal.require("settings.read")
        learning = self._learning()
        found: list[Offer] = []
        if self.intents is None:
            return found
        now = clock.now()
        resolver = await self.intents.resolver()
        zone = resolver.calendar.zone
        levels = await self.intents.levels()
        since = clock.time() - DISMISSED_S
        for keys in learning.hot_water.tanks():
            learned = learning.learnings.get(DEMAND, keys.scope)
            if learned is None or learned.state != "proved" or not isinstance(learned.value, dict):
                continue
            covered = set()
            for d in resolver.deadlines(now, now + timedelta(days=8)):
                if d.scope in (keys.scope, "house"):
                    local = d.t.astimezone(zone)
                    covered.add((resolver.calendar.weekday(local.date()), local.hour))
            dismissed = {
                str(b["id"])
                for _, b in await learning.hot_water.events(keys.scope, "offer_dismissed", since)
            }
            level = next(
                (lv.id for lv in levels.values() if lv.scope == keys.scope and lv.top is not None),
                None,
            )
            stop = self.values.latest.get(keys.stop)
            temp = float(stop.value) if stop and isinstance(stop.value, int | float) else 50.0
            week = Demand.from_value(learned.value)
            found += offers(keys.scope, week, covered, dismissed, level=level, temp=temp)
        return found

    async def accept_offer(self, caller: "Caller", id: str) -> dict[str, Any]:
        """Ask for an offered deadline, as the caller: with the caller's rights."""
        offer = next((o for o in await self.offers(caller) if o.id == id), None)
        if offer is None:
            raise AccountError("that suggestion isn't offered now")
        answer = await self.ask(caller, offer.request())
        if not answer["accepted"]:
            raise AccountError("; ".join(answer["messages"]) or "it wasn't accepted")
        return answer

    async def dismiss_offer(self, caller: "Caller", id: str) -> None:
        """Not now: the suggestion isn't made again for eight weeks."""
        caller.principal.require("settings.write")
        offer = next((o for o in await self.offers(caller) if o.id == id), None)
        if offer is None:
            raise AccountError("that suggestion isn't offered now")
        await self._learning().hot_water.keep(offer.scope, "offer_dismissed", {"id": id})
        await self.audit.record(
            _who(caller), "learned.offer_dismissed", details={"offer": offer.request()}
        )

    async def check_inputs(self, caller: "Caller") -> list[dict[str, Any]]:
        caller.principal.require("settings.write")
        learning = self._learning()
        found = await learning.check_inputs()
        await learning.learn()
        return [self._input(caller, c) for c in found]

    async def start_over(self, caller: "Caller", key: str) -> dict[str, Any]:
        caller.principal.require("settings.write")
        try:
            kept = await self._learning().learnings.start_over(key, _who(caller))
        except NotLearned:
            raise AccountError(f"nothing learned is called {key}") from None
        return self._value(caller, kept)

    async def pin_learned(self, caller: "Caller", key: str, value: float | None) -> dict[str, Any]:
        caller.principal.require("settings.write")
        try:
            kept = await self._learning().learnings.pin(key, value, _who(caller))
        except NotLearned:
            raise AccountError(f"nothing learned is called {key}") from None
        except ValueError as e:
            raise AccountError(str(e)) from None
        return self._value(caller, kept)

    def _input(self, caller: "Caller", c: "Check") -> dict[str, Any]:
        node = c.point.rpartition("/")[0]
        return {
            "instance": c.instance,
            "point": c.point,
            "label": self.label(c.instance, c.point),
            "part": self.node_label(caller, c.instance, node) if "/" in c.point else None,
            "verdict": c.verdict,
            "verdict_text": i18n._(VERDICTS[c.verdict]),
            "why": c.why,
            "args": dict(c.args),
            "why_text": _said(c.why, c.args),
            "checked": datetime.fromtimestamp(c.checked, UTC).isoformat(),
        }

    def _value(self, caller: "Caller", x: Learned) -> dict[str, Any]:
        instance, path = split(x.scope)
        out: dict[str, Any] = {
            "key": x.key,
            "name": x.name,
            "label": i18n._(NAMES.get(x.name, x.name)),
            "scope": x.scope,
            "part": self.node_label(caller, instance, path) if path else x.scope,
            "kind": x.kind,
            "value": x.value,
            "unit": x.unit,
            "shown": _shown(x),
            "starting": x.starting,
            "pinned": x.pinned,
            "used": x.used,
            "state": x.state,
            "state_text": i18n._(STATES[x.state]),
            "why": x.why,
            "args": dict(x.args),
            "why_text": _said(x.why, x.args) if x.why else None,
            "n": x.n,
            "since": _iso(x.since),
            "updated": _iso(x.updated),
            "pinnable": x.kind == "observed" or x.name in PINNABLE,
        }
        if x.name == "tank.demand" and isinstance(x.value, dict):
            out["week"] = week_rows(Demand.from_value(x.value))
        return out


def _who(caller: "Caller") -> str:
    return caller.principal.name


def _said(why: str, args: dict[str, float | int | str] | Any) -> str:
    """A reason in the page's language, its values in its formats: a moment as a time, a
    number with the page's decimal sign, a marked phrase translated."""
    shown = {}
    for k, v in args.items():
        if k == "seen" and isinstance(v, int | float):
            shown[k] = i18n.when(v)
        elif isinstance(v, int):
            shown[k] = i18n.number(v, 0)
        elif isinstance(v, float):
            shown[k] = i18n.number(v, 2 if abs(v) < 1 else 1)
        else:
            shown[k] = i18n._(v)
    return i18n._(why, **shown)


def _iso(t: float | None) -> str | None:
    return None if t is None else datetime.fromtimestamp(t, UTC).isoformat()


def _shown(x: Learned) -> str:
    value = x.value
    if value is None:
        return i18n.NOTHING
    if x.name == "tank.demand" and isinstance(value, dict):
        total = sum(Demand.from_value(value).hours)
        return i18n._(WEEK_SHOWN, total=i18n.number(total, 1))
    if x.name == "tank.loss" and isinstance(value, int | float):
        return i18n._(LOSS_SHOWN, fall=i18n.number(value * 30, 2))
    if x.name == "house.model" and isinstance(value, dict):
        return i18n._(MODEL_SHOWN, error=i18n.number(value.get("error", 0.0), 2))
    if x.name == "pump.response" and isinstance(value, dict):
        return i18n._(
            PUMP_SHOWN,
            step=i18n.number(value.get("c2", 0.0), 1),
            slope=i18n.number(-value.get("c1", 0.0), 2),
        )
    if x.name == "compressor.draw" and isinstance(value, dict):
        return ", ".join(
            i18n._(shown, kw=i18n.number(value[purpose], 2))
            for purpose, shown in DRAW_SHOWN.items()
            if purpose in value
        )
    if x.name == "compressor.starts" and isinstance(value, int | float):
        return i18n._(STARTS_SHOWN, starts=i18n.number(value, 1))
    if x.unit == "h" and isinstance(value, int | float):
        return i18n._(HOURS_SHOWN, hours=i18n.number(value, 0))
    if isinstance(value, dict):
        return ", ".join(i18n.when(t) for _, t in sorted(value.items(), key=lambda kv: -kv[1]))
    if x.unit == "s":
        return durations.text(value // 60 * 60)
    return i18n.number(value, 1) + (f" {x.unit}" if x.unit else "")


def week_rows(demand: Demand) -> list[dict[str, Any]]:
    """The week as rows of days and their hours, each shaded by how much is drawn in it."""
    most = max(demand.hours) or 1.0
    return [
        {
            "day": i18n.weekday(d),
            "seen": demand.seen[d],
            "hours": [
                {
                    "value": round(v, 2),
                    "shade": 0 if v <= 0 else min(SHADES, 1 + int(v / most * SHADES)),
                }
                for v in demand.day(d)
            ],
        }
        for d in range(7)
    ]
