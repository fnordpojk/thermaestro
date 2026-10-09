"""The plant: a house per climate system, the hot-water tank, a pool, and the pump that
heats them as an F1245 in auto mode does, roughly.

Everything the pump shows or is told is a register word in `registers` (16-bit) or
`registers32`, as on the bus: the settings are read from there at every step, so a write
over the bus takes effect, and the status registers follow the state. The physics is
simple on purpose (lumped nodes, explicit steps of at most 10 s); it is plausible, not
Nibe's own control, and it is not the planner's model of the house.

What it does, in short:
- each zone: indoor air, building mass and an emitter (radiators, or a slab that answers
  over a day or more), with outdoor temperature, sun and wind;
- the pump: the curve plus the offset gives the supply temperature it aims for; degree
  minutes add up the shortfall; the compressor starts at -60 and stops at 0; the addition
  starts deeper down, only below its stop temperature (47376) and within its power
  (47212); heating stops while the day's mean outdoor temperature is above 47375;
- hot water before heating: a charge starts when the charge sensor (BT6) falls to the
  mode's start temperature and ends at its stop; 48132 = 4 starts one at once; the
  periodic increase runs every 47051 days; while both are wanted, hot water and heating
  take turns (47134, 47135 minutes);
- a pool, heated last, between its start and stop while it is activated;
- the house's electricity: the pump's draw and the household's own load.
"""

import math
import random
from dataclasses import dataclass, field
from datetime import datetime

from thermaestro.nibe import profile

from .scenario import House, Scenario

STEP_S = 10.0
NOT_CONNECTED = 0x8000
WATER_J_KG_K = 4186.0
FLOW_W_K = 0.25 * WATER_J_KG_K
"""The heating medium's flow through the emitters, as heat per kelvin."""

CURVES = {1: 47007, 2: 47006, 3: 47005, 4: 47004, 5: 48489, 6: 48488, 7: 47567, 8: 47525}
"""Each climate system's curve register."""
START_DM, STOP_DM = -60.0, 0.0
ADDITION_DM = -400.0
MIN_OFF_S = 1200.0
"""The least time between a compressor stop and the next start."""
STARTING_S = 60.0
"""How long the compressor shows starting (40) or stopping (100)."""

DEFAULTS: dict[int, int] = {
    47011: 0,  # offset S1
    47041: 1,  # hot water: normal
    47045: 420,  # start and stop: economy 42/47, normal 45/50, luxury 48/53 °C
    47049: 470,
    47044: 450,
    47048: 500,
    47043: 480,
    47047: 530,
    47046: 550,  # the periodic increase's stop, 55 °C
    47050: 1,  # periodic increase on
    47051: 14,  # every 14 days
    47375: 170,  # heating stop, 17 °C
    47376: 50,  # addition stop, 5 °C
    47212: 650,  # internal addition at most 6.5 kW
    47137: 0,  # auto mode
    47134: 30,  # hot water and heating take turns of 30 min
    47135: 30,
    48132: 0,
    48852: 0,  # 32-bit values high word first
    43001: 9721,
    44331: 4,
    45001: 0,
    47394: 0,  # the pump's room control off
    47393: 0,
    48087: 0,  # no pool 2
    40106: NOT_CONNECTED,
}


def s16(value: float, factor: int = 10) -> int:
    raw = round(value * factor)
    return max(-0x7FFF, min(0x7FFF, raw)) & 0xFFFF


def signed(word: int, bits: int = 16) -> int:
    word &= (1 << bits) - 1
    return word - (1 << bits) if word & (1 << (bits - 1)) else word


@dataclass
class Zone:
    air: float
    mass: float
    emitter: float
    heat_w: float = 0.0
    """What the emitter got from the pump in the last step."""


@dataclass
class Plant:
    scenario: Scenario
    t: float
    """Seconds since the epoch."""
    registers: dict[int, int] = field(default_factory=dict)
    registers32: dict[int, int] = field(default_factory=dict)
    refuse: set[int] = field(default_factory=set)
    """Registers whose writes the pump refuses."""
    not_kept: set[int] = field(default_factory=set)
    """Registers whose writes the pump accepts and drops."""
    zones: list[Zone] = field(default_factory=list)
    top: float = 48.0
    bottom: float = 48.0
    pool: float | None = None
    dm: float = 0.0
    mean_out: float = 0.0
    compressor_on: bool = False
    compressor_since: float = -1e9
    addition_kw: float = 0.0
    demand: str = "idle"
    turn_since: float = 0.0
    charging: str | None = None
    """Why a hot-water charge runs: "start", "boost", "periodic"; None if none does."""
    pool_heating: bool = False
    last_periodic: float = 0.0
    lux_until: float | None = None
    run_s: float = 0.0
    """How long the compressor has run since it started."""
    house_kw: float = 0.0
    pump_kw: float = 0.0
    heat: dict[str, float] = field(default_factory=dict)
    """Heat delivered so far, kWh, by purpose."""
    _weather: "WeatherSeries | None" = None

    def __post_init__(self) -> None:
        s = self.scenario
        self._weather = WeatherSeries(s)
        self.mean_out = self._weather.outdoor(self.t)
        self.zones = [Zone(h.start_c, h.start_c, h.start_c + 5) for h in s.houses]
        self.top = self.bottom = s.tank.start_c
        if s.pool is not None:
            self.pool = s.pool.start_c
        rng = random.Random(s.seed + 7)
        self.last_periodic = self.t - rng.uniform(0, 13) * 86_400
        registers = dict(DEFAULTS)
        for system in profile.SYSTEMS:
            present = system.number <= len(s.houses)
            registers[system.supply] = 0 if present else NOT_CONNECTED
            if system.accessory is not None:
                registers[system.accessory] = int(present)
            if present:
                house = s.houses[system.number - 1]
                curve = curve_for(house)
                registers[CURVES[system.number]] = curve
                if system.offset is not None:
                    offset = offset_for(house, curve, s.weather.mean_c)
                    registers[system.offset] = offset & 0xFFFF
        if s.pool is not None:
            registers.update({48088: 1, 48090: 220, 48092: 280, 48094: 1})
        else:
            registers.update({48088: 0, 40042: NOT_CONNECTED})
        registers.update(s.registers)
        for register, word in registers.items():
            self.registers.setdefault(register, word & 0xFFFF)
        self.heat = {"heating": 0.0, "dhw": 0.0, "pool": 0.0}
        self._show()

    # --- reading settings ------------------------------------------------------------------

    def setting(self, register: int, factor: int = 10, bits: int = 16) -> float:
        return signed(self.registers.get(register, 0), bits) / factor

    def word(self, register: int) -> int:
        return self.registers.get(register, 0) & 0xFFFF

    @property
    def outdoor(self) -> float:
        assert self._weather is not None
        return self._weather.outdoor(self.t)

    def target(self, number: int) -> float:
        """The supply temperature the pump aims for in climate system `number`."""
        curve = signed(self.registers.get(CURVES[number], 9), 8)
        system = profile.SYSTEMS[number - 1]
        offset = signed(self.registers.get(system.offset or 0, 0), 8)
        return supply_target(curve, offset, self.outdoor)

    def hot_water_limits(self) -> tuple[float, float]:
        """The current mode's start and stop temperatures; luxury while temporary lux runs."""
        mode = signed(self.word(47041), 8)
        if self.lux_until is not None:
            mode = 2
        start, stop = {0: (47045, 47049), 1: (47044, 47048), 2: (47043, 47047)}.get(
            mode, (47044, 47048)
        )
        return self.setting(start), self.setting(stop)

    # --- writing ---------------------------------------------------------------------------

    def write(self, register: int, value: int) -> bool:
        """A write over the bus: whether the pump accepted it. Accepted isn't kept for
        `not_kept` registers; the addition's stop is kept no higher than the heating stop,
        as the manual says."""
        if register in self.refuse:
            return False
        if register in self.not_kept:
            return True
        word = value & 0xFFFF
        if register == 47376:
            word = s16(min(signed(word) / 10, self.setting(47375)))
        self.registers[register] = word
        return True

    # --- stepping --------------------------------------------------------------------------

    def run(self, seconds: float) -> None:
        """Move on by `seconds`, in steps of at most STEP_S."""
        end = self.t + seconds
        while self.t < end - 1e-9:
            self.step(min(STEP_S, end - self.t))

    def step(self, dt: float) -> None:
        s = self.scenario
        assert self._weather is not None
        out = self._weather.outdoor(self.t)
        self.mean_out += (out - self.mean_out) * dt / 86_400
        heating_allowed = self.mean_out < self.setting(47375) and self.word(47137) == 0
        self._hot_water_wanted()
        heat_needed = heating_allowed and self.dm <= START_DM
        pool_wanted = self._pool_wanted()
        # Who gets the compressor: hot water first, taking turns with heating.
        previous = self.demand
        if self.charging is not None and heat_needed:
            turn = self.setting(47134, 1) if previous == "dhw" else self.setting(47135, 1)
            if previous in ("dhw", "heating") and self.t - self.turn_since < turn * 60:
                demand = previous
            else:
                demand = "heating" if previous == "dhw" else "dhw"
        elif self.charging is not None:
            demand = "dhw"
        elif heating_allowed and (heat_needed or (self.compressor_on and self.dm < STOP_DM)):
            demand = "heating"
        elif pool_wanted:
            demand = "pool"
        else:
            demand = "idle"
        if demand != previous:
            self.turn_since = self.t
        self.demand = demand
        wants = demand != "idle"
        if wants and not self.compressor_on and self.t - self.compressor_since >= MIN_OFF_S:
            self.compressor_on, self.compressor_since, self.run_s = True, self.t, 0.0
        elif not wants and self.compressor_on:
            self.compressor_on, self.compressor_since = False, self.t
        if self.compressor_on:
            self.run_s += dt
        # The addition: deep degree minutes, below its stop temperature, within its power.
        allowed = heating_allowed and self.mean_out < self.setting(47376)
        most = max(0.0, self.setting(47212, 100))
        if demand == "heating" and allowed and self.dm <= ADDITION_DM:
            depth = (ADDITION_DM - self.dm) / 100 + 1
            self.addition_kw = min(most, round(depth) * most / 3)
        elif self.charging == "periodic" and self.bottom > 50:
            self.addition_kw = min(most, 3.0)
        else:
            self.addition_kw = 0.0
        brine_in = s.weather.ground_c - 2.0 * min(1.0, self.run_s / 3600)
        # Heat from the compressor, by where it goes.
        heat_w = 0.0
        cop = 1.0
        if self.compressor_on and demand != "idle":
            condensing = self.bottom + 7 if demand == "dhw" else self._supply_now() + 3
            heat_w = s.heat_kw * 1000 * (1 + 0.025 * brine_in) * (1 - 0.008 * (condensing - 35))
            if demand == "dhw":
                heat_w *= min(1.0, max(0.0, (63 - condensing) / 8))
            cop = carnot_cop(condensing, brine_in - 3)
        elec_w = (heat_w / cop if heat_w else 0.0) + self.addition_kw * 1000
        if self.compressor_on:
            elec_w += 80.0  # circulation pumps
        added_w = self.addition_kw * 1000
        for zone in self.zones:
            zone.heat_w = 0.0
        if demand == "heating":
            self._heat_zones(heat_w + added_w)
            self.heat["heating"] += (heat_w + added_w) * dt / 3.6e6
        elif demand == "dhw":
            self.bottom += (heat_w + added_w) * dt / (WATER_J_KG_K * self._liters(False))
            self.heat["dhw"] += (heat_w + added_w) * dt / 3.6e6
        elif demand == "pool" and self.pool is not None and s.pool is not None:
            self.pool += heat_w * dt / (WATER_J_KG_K * s.pool.m3 * 1000)
            self.heat["pool"] += heat_w * dt / 3.6e6
        self._houses(dt, out)
        self._tank(dt)
        if self.pool is not None and s.pool is not None:
            self.pool -= (
                s.pool.loss_w_k * (self.pool - out) * dt / (WATER_J_KG_K * s.pool.m3 * 1000)
            )
        # Degree minutes: the shortfall of the supply against the curve, per minute.
        if heating_allowed:
            self.dm += (self._supply_now() - self.target(1)) * dt / 60
            self.dm = max(-3000.0, min(100.0, self.dm))
        else:
            self.dm = 0.0
        self.pump_kw = elec_w / 1000
        self.house_kw = self.pump_kw + self._household_kw()
        self.t += dt
        self._show()

    def _supply_now(self) -> float:
        """BT2: the first zone's emitter, warmer by half the spread while heat flows."""
        zone = self.zones[0]
        return zone.emitter + zone.heat_w / (2 * FLOW_W_K)

    def _heat_zones(self, total_w: float) -> None:
        """The heat shared by how far each zone's emitter is below its curve."""
        short = [max(0.0, self.target(i + 1) - z.emitter) for i, z in enumerate(self.zones)]
        weights = short if sum(short) > 0 else [1.0] * len(self.zones)
        for zone, weight in zip(self.zones, weights, strict=True):
            zone.heat_w = total_w * weight / sum(weights)

    def _houses(self, dt: float, out: float) -> None:
        assert self._weather is not None
        sun = self._weather.sun(self.t)
        wind = self._weather.wind(self.t)
        for zone, house in zip(self.zones, self.scenario.houses, strict=True):
            to_air = house.ua_emitter_w_k * (zone.emitter - zone.air)
            from_mass = house.ua_mass_w_k * (zone.mass - zone.air)
            lost = (house.ua_w_k + house.wind_w_k_per_m_s * wind) * (zone.air - out)
            gains = house.solar_m2 * sun + house.internal_w
            zone.air += (to_air + from_mass - lost + gains) * dt / house.c_air_j_k
            zone.mass -= from_mass * dt / house.c_mass_j_k
            zone.emitter += (zone.heat_w - to_air) * dt / house.c_emitter_j_k

    def _liters(self, top: bool) -> float:
        tank = self.scenario.tank
        return tank.liters * (tank.top_share if top else 1 - tank.top_share)

    def _tank(self, dt: float) -> None:
        tank = self.scenario.tank
        flow = self._draw_l_s()
        if flow:
            self.top += flow * dt / self._liters(True) * (self.bottom - self.top)
            self.bottom += flow * dt / self._liters(False) * (tank.cold_c - self.bottom)
        for top in (True, False):
            share = tank.top_share if top else 1 - tank.top_share
            loss = tank.loss_w_k * share * ((self.top if top else self.bottom) - tank.room_c)
            delta = loss * dt / (WATER_J_KG_K * self._liters(top))
            if top:
                self.top -= delta
            else:
                self.bottom -= delta
        if self.bottom > self.top:  # warm water rises
            mixed = (self.top * self._liters(True) + self.bottom * self._liters(False)) / (
                tank.liters
            )
            self.top = self.bottom = mixed

    def _draw_l_s(self) -> float:
        when = datetime.fromtimestamp(self.t, self.scenario.start_time.tzinfo)
        hour = when.hour + when.minute / 60 + when.second / 3600
        flow = 0.0
        for draw in self.scenario.draws:
            if draw.hour <= hour < draw.hour + draw.minutes / 60:
                flow += draw.liters / (draw.minutes * 60)
        return flow

    def _hot_water_wanted(self) -> None:
        """Start or end a charge: by the mode's temperatures, a boost, or the periodic
        increase."""
        if self.lux_until is not None and self.t >= self.lux_until:
            self.lux_until = None
            self.registers[48132] = 0
        start, stop = self.hot_water_limits()
        boost = signed(self.word(48132), 8)
        if boost in (1, 2, 3) and self.lux_until is None:
            self.lux_until = self.t + {1: 3, 2: 6, 3: 12}[boost] * 3600
        if self.charging is None:
            if boost == 4 and self.bottom < self.setting(47047):
                self.charging = "boost"
            elif self.word(47050) and self._periodic_due():
                self.charging = "periodic"
            elif self.bottom <= start:
                self.charging = "start"
        elif self.charging == "boost":
            if self.bottom >= self.setting(47047):
                self.charging = None
                self.registers[48132] = 0
        elif self.charging == "periodic":
            if self.bottom >= self.setting(47046):
                self.charging = None
                self.last_periodic = self.t
        elif self.bottom >= stop:
            self.charging = None

    def _periodic_due(self) -> bool:
        days = max(1, self.word(47051))
        return self.t - self.last_periodic >= days * 86_400

    def _pool_wanted(self) -> bool:
        if self.pool is None or not self.word(48094):
            self.pool_heating = False
            return False
        if self.pool <= self.setting(48090):
            self.pool_heating = True
        elif self.pool >= self.setting(48092):
            self.pool_heating = False
        return self.pool_heating

    def _household_kw(self) -> float:
        load = self.scenario.load
        when = datetime.fromtimestamp(self.t, self.scenario.start_time.tzinfo)
        hour = when.hour + when.minute / 60
        kw = load.base_kw
        if 17 <= hour < 18:
            kw += load.cooking_kw
        rng = random.Random(self.scenario.seed * 100_003 + int(self.t // 900))
        if rng.random() < 0.15:
            kw += rng.uniform(0.5, 2.0)  # a kettle, a washing machine
        return kw

    # --- what the pump shows ---------------------------------------------------------------

    def _show(self) -> None:
        r = self.registers
        out = self.outdoor
        r[40004] = s16(out)
        r[40067] = s16(self.mean_out)
        for i, zone in enumerate(self.zones):
            system = profile.SYSTEMS[i]
            supply = zone.emitter + zone.heat_w / (2 * FLOW_W_K)
            r[system.supply] = s16(supply)
            if i == 0:
                r[40012] = s16(zone.emitter - zone.heat_w / (2 * FLOW_W_K))
        if self.demand == "dhw" and self.compressor_on:
            r[40008] = s16(self.bottom + 5)  # the water goes to the tank
        r[40013] = s16(self.top)
        r[40014] = s16(self.bottom)
        if self.pool is not None:
            r[40042] = s16(self.pool)
        brine_in = self.scenario.weather.ground_c - 2.0 * min(1.0, self.run_s / 3600)
        r[40015] = s16(brine_in)
        r[40016] = s16(brine_in - 3 if self.compressor_on else brine_in)
        r[43086] = {"idle": 10, "dhw": 20, "heating": 30, "pool": 40}[self.demand]
        since = self.t - self.compressor_since
        if self.compressor_on:
            r[43427] = 40 if since < STARTING_S else 60
        else:
            r[43427] = 100 if since < STARTING_S else 20
        r[43431] = 20
        r[43433] = 20
        r[43437] = 70 if self.compressor_on else 30
        r[43439] = 60 if self.compressor_on else 0
        r[43084] = s16(self.addition_kw, 100)
        r[43005] = s16(self.dm)
        self.registers32[42437] = round(self.heat["dhw"] * 10)
        self.registers32[42439] = round(self.heat["heating"] * 10)
        self.registers32[42443] = round(self.heat["pool"] * 10)

    # --- for tests and sensors -------------------------------------------------------------

    @property
    def time(self) -> datetime:
        return datetime.fromtimestamp(self.t, self.scenario.start_time.tzinfo)

    def indoor(self, number: int = 1) -> float:
        return self.zones[number - 1].air

    @property
    def systems(self) -> int:
        return len(self.zones)


def curve_for(house: House) -> int:
    """The curve that keeps the house about 21 °C: the emitter must be warmer than the air
    by the house's loss over the emitter's conductance."""
    slope = (house.ua_w_k / house.ua_emitter_w_k) * 1.3 + 0.1
    return max(1, min(15, round((slope - 0.06) / 0.09)))


def offset_for(house: House, curve: int, outdoor: float) -> int:
    """The offset a household would have trimmed the curve with, so that at the season's
    usual outdoor temperature the house sits near 21 °C: the supply the house needs, less
    what its own gains cover, against what the curve gives."""
    need_w = max(0.0, house.ua_w_k * (21 - outdoor) - house.internal_w)
    # The supply runs warmer than the emitter while the compressor runs, and the degree
    # minutes hold its average at the curve: about 2.5 K over the emitter's mean.
    supply = 21 + need_w / house.ua_emitter_w_k + 2.5
    step = 1 + 0.15 * curve
    return max(-5, min(5, round((supply - supply_target(curve, 0, outdoor)) / step)))


def supply_target(curve: int, offset: int, outdoor: float) -> float:
    """A curve's supply temperature: about 20 °C at 20 °C outdoors, rising with the curve's
    slope as it gets colder; an offset step moves it by more on a steeper curve."""
    slope = 0.09 * curve + 0.06
    target = 20 + slope * (20 - outdoor) + offset * (1 + 0.15 * curve)
    return max(20.0, min(65.0, target))


def carnot_cop(condensing: float, evaporating: float) -> float:
    """Half of Carnot's, within what a ground-source pump gets."""
    hot = condensing + 273.15
    lift = max(5.0, condensing - evaporating)
    return max(1.5, min(6.0, 0.5 * hot / lift))


class WeatherSeries:
    """Outdoor temperature, sun and wind, hour by hour from the scenario's seed."""

    def __init__(self, scenario: Scenario) -> None:
        self._scenario = scenario
        self._start = scenario.start_time.timestamp()
        self._days: dict[int, tuple[float, bool]] = {}
        self._hours: dict[int, tuple[float, float]] = {}

    def _day(self, index: int) -> tuple[float, bool]:
        """The day's mean temperature and whether it is clear."""
        if index not in self._days:
            w = self._scenario.weather
            rng = random.Random(self._scenario.seed * 7919 + index)
            wander = w.drift_c * math.sin(index / 3.0 + self._scenario.seed)
            self._days[index] = (w.mean_c + wander + rng.uniform(-1, 1), rng.random() < w.clear)
        return self._days[index]

    def _hour(self, index: int) -> tuple[float, float]:
        """The hour's noise on the temperature, and its wind."""
        if index not in self._hours:
            rng = random.Random(self._scenario.seed * 104_729 + index)
            wind = max(0.0, rng.gauss(self._scenario.weather.wind_m_s, 1.5))
            self._hours[index] = (rng.uniform(-0.4, 0.4), wind)
        return self._hours[index]

    def outdoor(self, t: float) -> float:
        hours = (t - self._start) / 3600
        day = math.floor(hours / 24)
        frac = hours / 24 - day
        mean = self._day(day)[0] * (1 - frac) + self._day(day + 1)[0] * frac
        clock = datetime.fromtimestamp(t, self._scenario.start_time.tzinfo)
        hour = clock.hour + clock.minute / 60
        swing = self._scenario.weather.swing_c * math.cos(2 * math.pi * (hour - 15) / 24)
        return mean + swing + self._hour(math.floor(hours))[0]

    def sun(self, t: float) -> float:
        """Irradiance on the sun's aperture, W/m², a winter's day from 08 to 16."""
        clock = datetime.fromtimestamp(t, self._scenario.start_time.tzinfo)
        hour = clock.hour + clock.minute / 60
        if not 8 <= hour <= 16:
            return 0.0
        day = math.floor((t - self._start) / 86_400)
        peak = 350.0 if self._day(day)[1] else 70.0
        return peak * math.sin(math.pi * (hour - 8) / 8)

    def wind(self, t: float) -> float:
        return self._hour(math.floor((t - self._start) / 3600))[1]


def begin(scenario: Scenario) -> Plant:
    """The plant at the scenario's start."""
    return Plant(scenario, scenario.start_time.timestamp())
