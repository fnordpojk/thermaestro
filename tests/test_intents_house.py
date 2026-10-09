"""Intents and the house they are about: what it has, read from the plugins, and the
defaults seeded from a day of the plant's readings."""

from pathlib import Path

from plant import draw
from plant.harness import Sim, running
from plant.vloop import simulate

from thermaestro.core import AuditLog
from thermaestro.core.house import House
from thermaestro.intents import Intents
from thermaestro.store import Home, Location

CS = "pump:hp1/cs1"
TANK = "pump:hp1/dhw"


def test_seeded_from_a_day_of_the_house(tmp_path: Path) -> None:
    scenario = draw(1, emitter="radiators").set({47442: 1})  # the pump's flow preset: radiators

    async def body() -> None:
        sim = Sim(scenario, tmp_path)
        async with running(sim):
            await sim.db.put(Location(latitude=57.7, longitude=12.0, timezone="Europe/Stockholm"))
            house = House(sim.db, sim.host, sim.values)
            caps = await house.refresh()
            assert caps.systems == {CS}
            assert caps.offset == {CS}
            assert caps.tanks == {TANK}
            assert caps.block == {TANK}
            assert caps.addition
            assert caps.power  # the plant's meter
            assert caps.pools == frozenset()
            intents = Intents(sim.db, AuditLog(tmp_path / "intents"), capabilities=lambda: caps)
            assert await intents.seed(await house.found()) != []  # nothing read yet: some
            await sim.advance(26 * 3600)
            await house.refresh()
            assert (await sim.db.get(Home) or Home()).emitters == {CS: "radiators"}
            seeded = await intents.seed(await house.found())
            every = await intents.all()
            assert {(i.kind, i.scope, i.confirmed) for i in every} == {
                ("comfort_band", CS, False),
                ("addition_policy", "house", False),
                ("hot_water_floor", TANK, False),
            }
            assert [i.kind for i in seeded] == ["hot_water_floor"]  # once a day was read
            no_sensor = next(i for i in every if i.kind == "comfort_band")
            assert no_sensor.parameters == {"no_sensor": True}  # no room sensor
            levels = await intents.levels()
            top = levels["current-pump-hp1-dhw"].top
            assert top is not None
            assert 44 <= top <= 56

    simulate(body, scenario.start_time)
