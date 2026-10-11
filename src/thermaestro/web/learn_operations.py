"""What Thermaestro has learned, and which inputs work here: the operations behind the
page and the API. Seeing them needs `settings.read`; checking the inputs again, starting a
value over or pinning one needs `settings.write`."""

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from .. import clock, durations
from ..auth import AccountError
from ..core.executor import split
from ..learn import Learned, NotLearned, mark
from ..learn.tank import Demand
from . import i18n

if TYPE_CHECKING:
    from ..learn import Check, Learning
    from .operations import Caller

NAMES = {
    "tank.charge_s": mark("How long a hot-water charge takes"),
    "tank.periodic": mark("The periodic hot-water increase, last seen"),
    "tank.loss": mark("The tank's standing loss"),
    "tank.demand": mark("Hot water drawn, through the week"),
    "tank.ceiling": mark("The warmest a charge has made the top"),
}
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
LOSS_SHOWN = mark("%(fall)s °C an hour at 50 °C")
WEEK_SHOWN = mark("%(total)s °C of the tank a week")
RECENT_DAYS = 7
SHADES = 4


class LearnOperations:
    """Mixed into the services, whose parts it uses."""

    if TYPE_CHECKING:
        learning: Learning | None

        def label(self, instance: str, path: str) -> str: ...

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
        return {
            "inputs": [self._input(caller, c) for c in checks],
            "values": values,
            "tanks": tanks,
        }

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
            "pinnable": x.kind == "observed" or x.name == "tank.loss",
        }
        if x.name == "tank.demand" and isinstance(x.value, dict):
            out["week"] = _week(Demand.from_value(x.value))
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
    if isinstance(value, dict):
        return ", ".join(i18n.when(t) for _, t in sorted(value.items(), key=lambda kv: -kv[1]))
    if x.unit == "s":
        return durations.text(value // 60 * 60)
    return i18n.number(value, 1) + (f" {x.unit}" if x.unit else "")


def _week(demand: Demand) -> list[dict[str, Any]]:
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
