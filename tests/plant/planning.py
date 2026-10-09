"""The planner on the plant: rooms with the plant's sensors, intents, prices, and the
planner checking in every minute as the daemon runs it."""

import asyncio
import json
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime

from thermaestro.auth.permissions import ALL
from thermaestro.core import AuditLog
from thermaestro.core.executor import COUNTED
from thermaestro.core.house import House
from thermaestro.core.sensors import SensorHub
from thermaestro.intents import Intent, Intents, Level
from thermaestro.planner import Planner, Price
from thermaestro.store import Location, Room, Sensor

from .harness import PLANT, SLOT_S, Sim
from .vloop import every

EVERYTHING = frozenset({ALL})
CS = "pump:hp1/cs1"
TANK = "pump:hp1/dhw"


@dataclass
class Planned:
    sim: Sim
    planner: Planner
    intents: Intents
    house: House
    hub: SensorHub


@asynccontextmanager
async def planning(
    sim: Sim,
    intents: Sequence[Intent],
    levels: Sequence[Level],
    prices: list[Price],
    *,
    rooms: bool = True,
) -> AsyncIterator[Planned]:
    """The planner around a started sim, ticking every minute while the context lasts."""
    await sim.db.put(Location(latitude=57.7, longitude=12.0, timezone="UTC"))
    hub = SensorHub(sim.db, sim.values)
    if rooms:
        for i in range(1, sim.plant.systems + 1):
            await sim.db.put(Room(name=f"Room {i}", climate_system=f"pump:hp1/cs{i}"), f"room{i}")
            await sim.db.put(
                Sensor(
                    name=f"Room {i}",
                    source="point",
                    point=f"{PLANT}:room.cs{i}/temperature",
                    room=f"room{i}",
                ),
                f"t{i}",
            )
    await hub.load()
    house = House(sim.db, sim.host, sim.values)
    audit = AuditLog(sim.path / "planner-audit")
    service = Intents(sim.db, audit, capabilities=lambda: house.capabilities)

    async def tick() -> None:
        hub.tick()
        await house.refresh()
        await service.advance()

    await asyncio.sleep(120)  # the plant's sensors report
    await tick()
    for level in levels:
        await service.put_level(level, who="user:test", granted=EVERYTHING)
    for intent in intents:
        verdict = await service.create(intent, granted=EVERYTHING)
        assert verdict.accepted, verdict.messages

    async def given(now: datetime) -> list[Price]:
        return prices

    planner = Planner(
        sim.db, sim.values, sim.host, sim.executor, service, house, audit, sensors=hub, prices=given
    )

    async def check() -> None:
        await tick()
        await planner.check()

    task = asyncio.create_task(every(60.0, check))
    try:
        yield Planned(sim, planner, service, house, hub)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def intended(sim: Sim) -> list[tuple[float, str, str, str]]:
    """The planner's intended writes, by slot: what was sent or, in shadow, would have been."""
    rows = await sim.db.run(
        lambda t: t.execute(
            "SELECT t, lever, op, params, outcome FROM acts WHERE who = 'planner' ORDER BY t, rowid"
        ).fetchall()
    )
    return [
        (t // SLOT_S * SLOT_S, lever, op, json.dumps(json.loads(params), sort_keys=True))
        for t, lever, op, params, outcome in rows
        if outcome in COUNTED
    ]
