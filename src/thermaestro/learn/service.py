"""Learning in the running core: what has been learned, and which inputs work here.

The inputs are checked once an hour, and when the household asks. What is found is kept
in the database, so the page and the learners see it after a restart."""

import asyncio
import json
import logging
from collections.abc import Iterator
from typing import cast

from .. import clock
from ..core.audit import AuditLog
from ..core.gridrules import GridLimits
from ..core.host import PluginHost
from ..core.sensors import SITE
from ..core.values import Key, Values
from ..store import Database, Transaction
from .inputs import (
    WINDOW_S,
    Check,
    Context,
    Expect,
    Recorded,
    Verdict,
    draws_with_compressor,
    follows_demand,
    judge,
    runs_and_stops,
    warmer_when_heating,
    warms_in_charges,
)
from .store import Learnings

log = logging.getLogger(__name__)

CHECK_S = 3600.0
FIRST_S = 600.0
"""The first check waits this long after a start: until then the devices may not have
said what they have, and what was kept from before stands."""

EXPECT: dict[tuple[str, str], Expect] = {
    ("unit", "demand"): Expect(),
    ("unit", "outdoor.temp"): Expect(),
    ("unit", "degree_minutes"): Expect(),
    ("compressor", "state"): Expect(relation=follows_demand),
    ("dhw_tank", "temp.top"): Expect(relation=warms_in_charges),
    ("dhw_tank", "temp.charge"): Expect(relation=warms_in_charges),
    ("climate_system", "supply.temp"): Expect(relation=warmer_when_heating),
    ("climate_system", "return.temp"): Expect(),
    ("addition", "power"): Expect(moves=False, relation=runs_and_stops),
    ("brine_circuit", "brine.in.temp"): Expect(),
    ("brine_circuit", "brine.out.temp"): Expect(),
}
"""The inputs the learners will use, by the kind of part they are on and their name, and
what each must have shown."""


class Learning:
    def __init__(self, db: Database, values: Values, host: PluginHost, audit: AuditLog) -> None:
        self._db = db
        self._values = values
        self._host = host
        self.learnings = Learnings(db, audit)
        self.checks: dict[Key, Check] = {}
        self._task: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()

    async def load(self) -> None:
        await self.learnings.load()

        def read(t: Transaction) -> dict[Key, Check]:
            rows = t.execute(
                "SELECT instance, point, verdict, why, checked, args FROM input_checks"
            )
            return {
                Key(i, p): Check(i, p, cast(Verdict, v), w, c, json.loads(a))
                for i, p, v, w, c, a in rows
            }

        self.checks = await self._db.run(read)

    def start(self) -> None:
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

    async def _run(self) -> None:
        await asyncio.sleep(FIRST_S)
        while True:
            try:
                await self.check_inputs()
            except Exception:
                log.exception("checking the learners' inputs failed; trying again later")
            await asyncio.sleep(CHECK_S)

    def usable(self, key: Key) -> bool:
        found = self.checks.get(key)
        return found is not None and found.verdict == "usable"

    async def check_inputs(self) -> list[Check]:
        """Judge every input over the last week, and keep what is found."""
        async with self._lock:
            now = clock.time()
            recorded = Recorded(self._values, now - WINDOW_S, now)
            found = []
            for key, expect, context in self._inputs():
                found.append(await judge(key, expect, recorded, context, now))
            await self._db.run(lambda t: _keep(t, found))
            self.checks = {Key(c.instance, c.point): c for c in found}
            return found

    def _inputs(self) -> Iterator[tuple[Key, Expect, Context]]:
        house = GridLimits(self._db, self._values).house_power()
        for instance_id, instance in sorted(self._host.instances.items()):
            described = instance.described
            if described is None:
                continue
            kinds = {n.path: n.kind for n in described.nodes}
            paths = {p.path for p in described.points}
            for unit in (n.path for n in described.nodes if n.kind == "unit"):
                context = Context(
                    demand=_key(instance_id, f"{unit}/demand", paths),
                    compressor=next(
                        (
                            Key(instance_id, p)
                            for p in sorted(paths)
                            if p.startswith(f"{unit}/")
                            and p.endswith("/state")
                            and kinds.get(p.rpartition("/")[0]) == "compressor"
                        ),
                        None,
                    ),
                    house=house,
                )
                for path in sorted(paths):
                    if not path.startswith(f"{unit}/"):
                        continue
                    node, _, name = path.rpartition("/")
                    expect = EXPECT.get((kinds.get(node, ""), name))
                    if expect is not None:
                        yield Key(instance_id, path), expect, context
                if house is not None and context.compressor is not None:
                    yield house[0], Expect(relation=draws_with_compressor), context
        for key in sorted(self._values.latest, key=lambda k: k.point):
            if key.instance != SITE:
                continue
            if key.point == "outdoor/temperature" or (
                key.point.startswith("room.") and key.point.endswith("/temperature")
            ):
                yield key, Expect(), Context()


def _key(instance: str, path: str, paths: set[str]) -> Key | None:
    return Key(instance, path) if path in paths else None


def _keep(t: Transaction, found: list[Check]) -> None:
    t.execute("DELETE FROM input_checks")
    t.executemany(
        "INSERT INTO input_checks (instance, point, verdict, why, args, checked)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        [
            (c.instance, c.point, c.verdict, c.why, json.dumps(dict(c.args)), c.checked)
            for c in found
        ],
    )
