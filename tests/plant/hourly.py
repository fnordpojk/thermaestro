"""The plant an hour at a time, as the house's learners read it: the rooms, the outdoor
temperature, the water and the supply, the offset and the share of the hour the compressor
heated; and what nothing on a real pump measures, the heat it gave."""

import random
from dataclasses import dataclass, field

from thermaestro.learn.cop import carnot
from thermaestro.learn.house import Hour

from .model import FLOW_W_K, Plant, signed


@dataclass
class Recorded:
    hours: list[Hour]
    heat_kw: list[float]
    """The heat the pump gave the house each hour, kWh."""
    drawn_kw: list[float]
    """What the pump drew while the compressor heated the house, each minute it did."""
    dhw_kw: list[float] = field(default_factory=list)
    """And while it charged the tank."""
    heating_share: list[float] = field(default_factory=list)
    """The compressor's COP as a share of the ideal, heating: each minute it did."""
    dhw_share: list[float] = field(default_factory=list)


def hour(plant: Plant, offset: int, out: Recorded) -> None:
    """One more hour at `offset`, recorded."""
    weather = plant._weather
    assert weather is not None
    plant.registers[47011] = offset & 0xFFFF
    t0, given = plant.t, plant.heat["heating"]
    sums = [0.0] * 5
    for _ in range(60):
        plant.run(60)
        zone = plant.zones[0]
        sums[0] += zone.air
        sums[1] += plant.outdoor
        sums[2] += zone.emitter
        sums[3] += zone.emitter + zone.heat_w / (2 * FLOW_W_K)
        heating = plant.compressor_on and plant.demand == "heating"
        sums[4] += heating
        brine = signed(plant.registers[40015]) / 10
        if heating and plant.addition_kw == 0:
            out.drawn_kw.append(plant.pump_kw)
            if plant.cop:
                supply = zone.emitter + zone.heat_w / (2 * FLOW_W_K)
                out.heating_share.append(plant.cop / carnot(supply, brine))
        if plant.compressor_on and plant.demand == "dhw" and plant.addition_kw == 0:
            out.dhw_kw.append(plant.pump_kw)
            if plant.cop:
                out.dhw_share.append(plant.cop / carnot(plant.bottom + 5, brine))
    out.hours.append(
        Hour(
            t0,
            sums[0] / 60,
            sums[1] / 60,
            sums[2] / 60,
            weather.sun(t0),
            weather.wind(t0),
            None,
            sums[3] / 60,
            offset,
            sums[4] / 60,
        )
    )
    out.heat_kw.append(plant.heat["heating"] - given)


def shifted(plant: Plant, days: int, seed: int) -> Recorded:
    """`days` with the offset moved every few hours, as price shifting would."""
    rng = random.Random(seed)
    out = Recorded([], [], [])
    offset = 0
    for i in range(days * 24):
        if i % 4 == 0:
            offset = rng.randint(-3, 3)
        hour(plant, offset, out)
    return out
