"""What a climate system's emitters give the house per degree of water over the room: the
radiators' or the floor's output, in kW per kelvin.

The house model (`house`) works in temperatures: the water drives the rooms. What heating
costs is in heat, so the optimizer needs the heat the water gives, `K · (water - room)`.
Nothing measures that heat, but in the hours the compressor heated the house it can be
worked out: its electricity (as metered, or the learned draw times how long it ran) times
its COP there (the heating share of the ideal, learned or documented). `K` is fitted over
those hours, through the origin.

Only on a pump with one climate system: with two, the hour's heat can't be split between
them. Judged on the last four days, each hour's heat predicted from `K`, against the mean
heat of the hours before.
"""

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from .. import clock
from ..core.host import PluginHost
from ..store import Database, Transaction
from .efficiency import HEATING, _unbody
from .energy import DRAW
from .heating import DAYS
from .model import Learned, State, mark
from .store import Learnings
from .validate import held_out

EMITTERS = "house.emitter_kw_k"
LEAST = 24
"""Hours with heat known a value needs."""
TEST_HOURS = 96
WARMER_LEAST = 0.5
"""An hour counts only with the water this much warmer than the room."""

FEW = mark("%(hours)s hours of heating with the heat known, of %(least)s.")
SEVERAL = mark("Not learned: the pump heats several climate systems, and its heat can't be split.")
NO_COP = mark("Learned once the compressor's draw and COP are known.")


@dataclass(frozen=True, slots=True)
class Pair:
    t: float
    warmer: float
    """The water over the room, °C."""
    heat_kw: float


def fit(pairs: Sequence[Pair]) -> float | None:
    kept = [p for p in pairs if p.warmer >= WARMER_LEAST]
    if len(kept) < LEAST:
        return None
    k = sum(p.warmer * p.heat_kw for p in kept) / sum(p.warmer**2 for p in kept)
    return k if k > 0 else None


def judge(pairs: Sequence[Pair], test_from: float) -> tuple[float | None, Any]:
    train = [p for p in pairs if p.t < test_from and p.warmer >= WARMER_LEAST]
    test = [p for p in pairs if p.t >= test_from and p.warmer >= WARMER_LEAST]
    k = fit(train)
    verdict = None
    if k is not None and train:
        mean = sum(p.heat_kw for p in train) / len(train)
        verdict = held_out(
            [k * p.warmer for p in test], [mean] * len(test), [p.heat_kw for p in test]
        )
    return fit(pairs), verdict


class Emitters:
    def __init__(
        self,
        db: Database,
        host: PluginHost,
        learnings: Learnings,
        systems: Callable[[], Any],
    ) -> None:
        self._db = db
        self._host = host
        self._learnings = learnings
        self._systems = systems
        """The climate systems as the heating learner finds them."""

    async def run(self, now: float | None = None) -> None:
        now = clock.time() if now is None else now
        found = await self._systems()
        by_unit: dict[str, list[Any]] = {}
        for keys in found:
            instance, path = keys.scope.split(":", 1)
            unit = f"{instance}:{path.rpartition('/')[0]}"
            by_unit.setdefault(unit, []).append(keys)
        for unit, systems in by_unit.items():
            for keys in systems:
                if not keys.rooms:
                    continue
                if len(systems) > 1:
                    await self._put(keys.scope, None, "learning", SEVERAL, {}, 0)
                    continue
                await self._learn(keys.scope, unit, now)

    async def _learn(self, scope: str, unit: str, now: float) -> None:
        draw = self._learnings.used(DRAW, unit)
        heat_kw = draw.get("heating") if isinstance(draw, dict) else None
        share = self._learnings.used(HEATING, unit)
        since = now - DAYS * 86_400
        runs = {t: _unbody(t, b) for t, b in await self._events(unit, "cop_hour", since)}
        metered = bool(runs) and all(r.measured_kwh is not None for r in runs.values())
        if not isinstance(share, int | float) or (heat_kw is None and not metered):
            await self._put(scope, None, "learning", NO_COP, {}, 0)
            return
        rooms: dict[float, float] = {}
        for _, body in await self._events(scope, "house_day", since):
            for h in body["hours"]:
                if h.get("skip") or h.get("room") is None or h.get("water") is None:
                    continue
                rooms[float(h["t"])] = h["water"] - h["room"]
        pairs = [
            Pair(t, rooms[t], run.kwh(heat_kw or 0.0) * float(share) * run.ideal)
            for t, run in sorted(runs.items())
            if t in rooms
        ]
        k, verdict = judge(pairs, now - TEST_HOURS * 3600)
        if k is None:
            kept = sum(1 for p in pairs if p.warmer >= WARMER_LEAST)
            await self._put(scope, None, "learning", FEW, {"hours": kept, "least": LEAST}, kept)
            return
        state: State = "proved" if verdict is not None and verdict.proved else "learning"
        why = verdict.why if verdict is not None else None
        args = dict(verdict.args) if verdict is not None else {}
        await self._put(scope, round(k, 4), state, why, args, len(pairs))

    async def _put(
        self,
        scope: str,
        value: float | None,
        state: State,
        why: str | None,
        args: dict[str, float | int | str],
        n: int,
    ) -> None:
        before = self._learnings.get(EMITTERS, scope)
        if value is None and before is not None and before.value is not None:
            return  # kept as it was learned
        await self._learnings.put(
            Learned(
                name=EMITTERS,
                scope=scope,
                kind="model",
                value=value,
                unit="kW/K",
                state=state,
                why=why,
                args=args,
                n=n,
                since=before.since if before and before.since else None,
            )
        )

    async def _events(
        self, scope: str, kind: str, since: float
    ) -> list[tuple[float, dict[str, Any]]]:
        def read(t: Transaction) -> list[tuple[float, dict[str, Any]]]:
            rows = t.execute(
                "SELECT t, body FROM learn_events WHERE scope = ? AND kind = ? AND t >= ?"
                " ORDER BY t",
                (scope, kind, since),
            )
            return [(at, json.loads(body)) for at, body in rows]

        return await self._db.run(read)
