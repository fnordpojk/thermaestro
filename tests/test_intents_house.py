"""Intents and the house they are about: what it has, read from the plugins, and the
defaults seeded from the pump's own settings or a day of the plant's readings."""

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


def test_seeded_from_the_pumps_settings(tmp_path: Path) -> None:
    """The tank's floor and level from the mode's start and stop at once; the comfort band
    without a room sensor at once too."""
    scenario = draw(1, emitter="radiators")

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
            await sim.advance(600)
            seeded = await intents.seed(await house.found())
            assert {(i.kind, i.scope, i.confirmed) for i in seeded} == {
                ("comfort_band", CS, False),
                ("addition_policy", "house", False),
                ("hot_water_floor", TANK, False),
            }
            floor = next(i for i in seeded if i.kind == "hot_water_floor")
            assert floor.expectations[0].targets[0].value == 45.0  # Normal's start
            assert (await intents.levels())["current-pump-hp1-dhw"].top == 50.0  # its stop
            no_sensor = next(i for i in seeded if i.kind == "comfort_band")
            assert no_sensor.parameters == {"no_sensor": True}  # no room sensor
            await sim.advance(26 * 3600)
            await house.refresh()
            assert (await sim.db.get(Home) or Home()).emitters == {}  # setup asks
            assert await intents.seed(await house.found()) == []

    simulate(body, scenario.start_time)


def test_seeded_from_a_day_where_the_pump_doesnt_say(tmp_path: Path) -> None:
    """In Smart Control the pump has no start or stop of its own: the tank's top over a
    day instead."""
    scenario = draw(1, emitter="radiators").set({47041: 4})

    async def body() -> None:
        sim = Sim(scenario, tmp_path)
        async with running(sim):
            house = House(sim.db, sim.host, sim.values)
            caps = await house.refresh()
            intents = Intents(sim.db, AuditLog(tmp_path / "intents"), capabilities=lambda: caps)
            await sim.advance(600)
            first = await intents.seed(await house.found())
            assert "hot_water_floor" not in {i.kind for i in first}  # nothing read yet
            await sim.advance(26 * 3600)
            await house.refresh()
            seeded = await intents.seed(await house.found())
            assert [i.kind for i in seeded] == ["hot_water_floor"]  # once a day was read
            top = (await intents.levels())["current-pump-hp1-dhw"].top
            assert top is not None
            assert 44 <= top <= 56

    simulate(body, scenario.start_time)
