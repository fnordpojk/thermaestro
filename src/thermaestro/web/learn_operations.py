"""What Thermaestro has learned, and which inputs work here: the operations behind the
page and the API. Seeing them needs `settings.read`; checking the inputs again, starting a
value over or pinning one needs `settings.write`."""

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from .. import durations
from ..auth import AccountError
from ..core.executor import split
from ..learn import Learned, NotLearned, mark
from . import i18n

if TYPE_CHECKING:
    from ..learn import Check, Learning
    from .operations import Caller

NAMES = {
    "tank.charge_s": mark("How long a hot-water charge takes"),
    "tank.periodic": mark("The periodic hot-water increase, last seen"),
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

    def learned(self, caller: "Caller") -> dict[str, Any]:
        """Which inputs work here, and what has been learned."""
        caller.principal.require("settings.read")
        learning = self._learning()
        checks = sorted(learning.checks.values(), key=lambda c: (c.instance, c.point))
        return {
            "inputs": [self._input(caller, c) for c in checks],
            "values": [self._value(caller, x) for x in learning.learnings.all()],
        }

    async def check_inputs(self, caller: "Caller") -> list[dict[str, Any]]:
        caller.principal.require("settings.write")
        found = await self._learning().check_inputs()
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
        args = {k: _arg(v) for k, v in c.args.items()}
        return {
            "instance": c.instance,
            "point": c.point,
            "label": self.label(c.instance, c.point),
            "part": self.node_label(caller, c.instance, node) if "/" in c.point else None,
            "verdict": c.verdict,
            "verdict_text": i18n._(VERDICTS[c.verdict]),
            "why": c.why,
            "args": dict(c.args),
            "why_text": i18n._(c.why, **args),
            "checked": datetime.fromtimestamp(c.checked, UTC).isoformat(),
        }

    def _value(self, caller: "Caller", x: Learned) -> dict[str, Any]:
        instance, path = split(x.scope)
        return {
            "key": x.key,
            "name": x.name,
            "label": i18n._(NAMES.get(x.name, x.name)),
            "scope": x.scope,
            "part": self.node_label(caller, instance, path) if path else x.scope,
            "kind": x.kind,
            "value": x.value,
            "unit": x.unit,
            "shown": _shown(x.value, x.unit),
            "starting": x.starting,
            "pinned": x.pinned,
            "used": x.used,
            "state": x.state,
            "state_text": i18n._(STATES[x.state]),
            "why": x.why,
            "why_text": i18n._(x.why) if x.why else None,
            "n": x.n,
            "since": _iso(x.since),
            "updated": _iso(x.updated),
            "pinnable": not isinstance(x.value, dict),
        }


def _who(caller: "Caller") -> str:
    return caller.principal.name


def _arg(v: float | int | str) -> str:
    """A value in a check's reason, in the page's number format."""
    if isinstance(v, int):
        return i18n.number(v, 0)
    if isinstance(v, float):
        return i18n.number(v, 2 if abs(v) < 1 else 1)
    return v


def _iso(t: float | None) -> str | None:
    return None if t is None else datetime.fromtimestamp(t, UTC).isoformat()


def _shown(value: float | dict[str, float] | None, unit: str | None) -> str:
    if value is None:
        return i18n.NOTHING
    if isinstance(value, dict):
        return ", ".join(i18n.when(t) for _, t in sorted(value.items(), key=lambda kv: -kv[1]))
    if unit == "s":
        return durations.text(value // 60 * 60)
    return i18n.number(value, 1) + (f" {unit}" if unit else "")
