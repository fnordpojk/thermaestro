"""The pump profiles: Nibe registers as the capability interface's points and levers, with
the rules that say how far each value can be trusted.

A `Family` holds one family's profile: the bus family's is here (`BUS`), the S-series' in
`sprofile`. The profile names registers; the model's register map says whether the model
has them and how to decode them. A point whose register the model lacks isn't described.
"""

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from .. import durations
from ..cap.model import (
    CompetingFeature,
    Implementation,
    Knowledge,
    Lever,
    Param,
    Persistence,
    Quality,
    Range,
    Source,
    Verify,
    Wear,
)
from .maps import ModelMap

UNIT = "hp1"

PRIO = 43086
COMPRESSOR = 43427
SUPPLY_PUMP_SPEED = 43437
BRINE_PUMP_SPEED = 43439
BRINE_OUT_LIMIT = 47381
"""The pump's own low brine-out alarm limit (°C, factory -8)."""
WORD_SWAP = 48852
FIRMWARE = (43001, 44331)

PRIO_HOT_WATER = 20
COMPRESSOR_CHANGING = frozenset({40, 100})
COMPRESSOR_RUNNING = 60
CHARGE_SETTLE_S = 600.0
"""How long the charge sensor (BT6) reads low after a charge starts: water from the bottom
of the tank passes it. Ten minutes for now; the real time is to be learned per tank."""

DEMAND = {
    10: "idle",
    20: "dhw",
    30: "heating",
    40: "pool",
    41: "pool",
    50: "transfer",
    60: "cooling",
}
COMPRESSOR_STATE = {20: "stopped", 40: "starting", 60: "running", 100: "stopping"}
PUMP_STATE = {10: "off", 15: "starting", 20: "on", 40: "10-day mode", 80: "calibration"}

UNITS = {"°C": "degC"}


@dataclass(frozen=True, slots=True)
class Snapshot:
    """What the plugin last read, for the rules: decoded values by register, and when a
    hot-water charge last started (monotonic seconds), if one is running."""

    values: Mapping[int, float | int | None]
    charge_started: float | None
    now: float
    idle: Mapping[int, float] = field(default_factory=dict)
    """For each heat meter: seconds the pump has produced for its purpose since the meter
    last changed."""


METERS = {
    42437: "dhw",
    42445: "dhw",
    42439: "heating",
    42447: "heating",
    42441: "cooling",
    42443: "pool",
}
"""The heat meters, and what they count: production for this demand."""
METER_IDLE_S = 1800.0
"""A meter that hasn't moved in this much production doesn't count on this pump: some
answer with a value that never changes (an F1245's hot-water total, over several
hot-water runs)."""


Rule = Callable[[Snapshot], tuple[Quality, str] | None]
"""A validity rule: a quality and its reason where the value can't be fully trusted, or
None where the rule has nothing to say."""


def counting(register: int, purpose: str) -> Rule:
    def rule(s: Snapshot) -> tuple[Quality, str] | None:
        idle = s.idle.get(register, 0.0)
        if idle >= METER_IDLE_S:
            span = durations.text(idle // 60 * 60)
            return "unknown", f"hasn't changed in {span} of {purpose} production"
        return None

    return rule


SUPPLY_STOPPED = "the heating medium pump (GP1) is stopped, so the water at the sensor stands still"
BRINE_STOPPED = "the brine pump (GP2) is stopped, so the brine at the sensor stands still"


def no_flow(pump: int, why: str) -> Rule:
    """No flow while the circulation pump `pump` stands still; `why` says which."""

    def rule(s: Snapshot) -> tuple[Quality, str] | None:
        if s.values.get(pump) == 0:
            return "no_flow", why
        return None

    return rule


def changing(compressor: int, states: Mapping[int, str], moving: frozenset[int]) -> Rule:
    """Transitional while the compressor's state is one of `moving` (starting, stopping)."""

    def rule(s: Snapshot) -> tuple[Quality, str] | None:
        state = s.values.get(compressor)
        if state is not None and int(state) in moving:
            return "transitional", f"compressor {states[int(state)]}"
        return None

    return rule


def charge_starting(s: Snapshot) -> tuple[Quality, str] | None:
    if s.charge_started is not None and s.now - s.charge_started < CHARGE_SETTLE_S:
        return "transitional", "a hot-water charge started"
    return None


def diverted(register: int, hot_water: int) -> Rule:
    """While `register` says the water goes to the tank."""

    def rule(s: Snapshot) -> tuple[Quality, str] | None:
        if s.values.get(register) == hot_water:
            # True, but it describes the charge, not the heating.
            return "good", "the water goes to the hot-water tank now, not to the heating"
        return None

    return rule


compressor_changing = changing(COMPRESSOR, COMPRESSOR_STATE, COMPRESSOR_CHANGING)
diverted_to_hot_water = diverted(PRIO, PRIO_HOT_WATER)


@dataclass(frozen=True, slots=True)
class PointDef:
    path: str
    """Below the unit: `outdoor.temp`, `cs1/supply.temp`."""
    register: int
    enum: Mapping[int, str] | None = None
    rules: tuple[Rule, ...] = ()
    validity: tuple[str, ...] = ()
    """The rules in words, for explanations."""
    source: Source = "measured"
    unknown_why: str | None = None
    """Why a device value outside `enum` gives no value, where that is expected."""


@dataclass(frozen=True, slots=True)
class System:
    number: int
    supply: int
    accessory: int | None
    """The register that switches the climate system on; None for system 1, always there."""
    offset: int | None = None
    room_control: int | None = None


SYSTEMS = (
    System(1, 40008, None, offset=47011, room_control=47394),
    System(2, 40007, 47302, offset=47010, room_control=47393),
    System(3, 40006, 47303, offset=47009, room_control=47392),
    System(4, 40005, 47304, offset=47008, room_control=47391),
    System(5, 40162, 48569, offset=48494, room_control=48678),
    System(6, 40161, 48570, offset=48493, room_control=48677),
    System(7, 40160, 48571, offset=48492, room_control=48676),
    System(8, 40159, 48572, offset=48491, room_control=48675),
)


@dataclass(frozen=True, slots=True)
class Pool:
    """A pool heated by a POOL 40 accessory."""

    number: int
    accessory: int
    """The register that switches the accessory on."""
    sensor: int
    """The pool's temperature sensor, BT51."""
    start: int
    stop: int
    activated: int
    """Pool heating on or off, as in the pump's menu."""


POOLS = (
    Pool(1, 48088, 40042, start=48090, stop=48092, activated=48094),
    Pool(2, 48087, 40106, start=48089, stop=48091, activated=48093),
)


def pool_points(pool: Pool) -> list[PointDef]:
    node = f"pool{pool.number}"
    return [
        PointDef(f"{node}/temp", pool.sensor),
        *(PointDef(f"{node}/x.nibe.{r}", r) for r in (pool.start, pool.stop, pool.activated)),
    ]


FLOW_RULES = (no_flow(SUPPLY_PUMP_SPEED, SUPPLY_STOPPED), compressor_changing)
BRINE_RULES = (no_flow(BRINE_PUMP_SPEED, BRINE_STOPPED), compressor_changing)


def system_points(system: System) -> list[PointDef]:
    cs = f"cs{system.number}"
    points = [
        PointDef(
            f"{cs}/supply.temp",
            system.supply,
            rules=(*FLOW_RULES, diverted_to_hot_water) if system.number == 1 else (),
            validity=(
                (
                    f"no_flow when supply pump {SUPPLY_PUMP_SPEED} is 0",
                    f"transitional while compressor {COMPRESSOR} starts or stops",
                    "during a hot-water charge it describes the charge, not the heating",
                )
                if system.number == 1
                else ()
            ),
        )
    ]
    if system.offset is not None:
        points.append(PointDef(f"{cs}/x.nibe.{system.offset}", system.offset))
    if system.room_control is not None:
        points.append(PointDef(f"{cs}/x.nibe.{system.room_control}", system.room_control))
    return points


UNIT_POINTS = (
    PointDef("outdoor.temp", 40004),
    PointDef("demand", PRIO, enum=DEMAND),
    PointDef(
        "diverter",
        PRIO,
        enum={20: "dhw", 30: "heating", 60: "heating"},
        source="calculated",
        unknown_why="the bus-family pumps don't report the valve; known only while the demand says",
    ),
    PointDef("degree_minutes", 43005),
    PointDef("alarm", 45001),
    *(
        PointDef(
            path,
            register,
            rules=(counting(register, METERS[register]),),
            validity=(
                f"unknown when it hasn't changed in {durations.text(METER_IDLE_S)} of production",
            ),
        )
        for path, register in (
            ("heat.produced{purpose=dhw,by=total}", 42437),
            ("heat.produced{purpose=heating,by=total}", 42439),
            ("heat.produced{purpose=cooling,by=compressor}", 42441),
            ("heat.produced{purpose=pool,by=compressor}", 42443),
            ("heat.produced{purpose=dhw,by=compressor}", 42445),
            ("heat.produced{purpose=heating,by=compressor}", 42447),
        )
    ),
    PointDef(f"x.nibe.{WORD_SWAP}", WORD_SWAP),
    PointDef("x.nibe.47375", 47375),  # heating stop
    PointDef("x.nibe.47134", 47134),  # operating priority, hot water
    PointDef("x.nibe.47137", 47137),  # operating mode
    PointDef("x.nibe.47370", 47370),  # addition allowed
)

CS1_POINTS = (
    PointDef(
        "cs1/return.temp",
        40012,
        rules=FLOW_RULES,
        validity=(f"no_flow when supply pump {SUPPLY_PUMP_SPEED} is 0",),
    ),
    PointDef("cs1/pump.state", 43431, enum=PUMP_STATE),
    PointDef("cs1/pump.speed", SUPPLY_PUMP_SPEED),
)

DHW_POINTS = (
    PointDef("dhw/temp.top", 40013),
    PointDef(
        "dhw/temp.charge",
        40014,
        rules=(charge_starting,),
        validity=("transitional for a while after a charge starts: tank-bottom water passes it",),
    ),
    PointDef("dhw/x.nibe.47041", 47041),  # comfort mode
    *(PointDef(f"dhw/x.nibe.{r}", r) for r in (47043, 47044, 47045, 47047, 47048, 47049)),
    PointDef("dhw/x.nibe.47046", 47046),  # periodic stop temperature
    PointDef("dhw/x.nibe.47050", 47050),  # periodic on
    PointDef("dhw/x.nibe.47051", 47051),  # periodic interval
    PointDef("dhw/x.nibe.48132", 48132),  # temporary lux
    PointDef("dhw/x.nibe.47387", 47387),
)

COMPRESSOR_POINTS = (PointDef("compressor.ep14/state", COMPRESSOR, enum=COMPRESSOR_STATE),)

BRINE_POINTS = (
    PointDef(
        "brine/brine.in.temp",
        40015,
        rules=BRINE_RULES,
        validity=(f"no_flow when brine pump {BRINE_PUMP_SPEED} is 0",),
    ),
    PointDef(
        "brine/brine.out.temp",
        40016,
        rules=BRINE_RULES,
        validity=(f"no_flow when brine pump {BRINE_PUMP_SPEED} is 0",),
    ),
    PointDef("brine/pump.state", 43433, enum=PUMP_STATE),
    PointDef("brine/pump.speed", BRINE_PUMP_SPEED),
    # The pump's own: its low brine-out alarm limit, and the delta-T it holds the brine
    # pump to (the set point it works to, a fixed one, and which of them it uses).
    PointDef(f"brine/x.nibe.{BRINE_OUT_LIMIT}", BRINE_OUT_LIMIT),
    PointDef("brine/x.nibe.44911", 44911),
    PointDef("brine/x.nibe.49192", 49192),
    PointDef("brine/x.nibe.49193", 49193),
)

BRINE_IN, BRINE_OUT = 40015, 40016
BRINE_WARNING_K = 2.0
"""How close to the pump's own low brine-out limit Thermaestro warns, while the
compressor runs: before the pump's alarm."""
BRINE_WARNING_CLEAR_K = 2.5
"""Where the warning ends again, a little above where it starts, so it doesn't flicker."""

BRINE_HEAT = {
    "ethanol28": 4.08,
    "propylene_glycol30": 3.92,
    "ethylene_glycol30": 3.82,
}
"""What a liter of brine carries per kelvin, kJ/(L·K), at about 0 °C: CoolProp 8.0's
incompressible-fluid fits (MEA, MPG, MEG) to Melinder's property tables. The mixes
differ by a few percent, much less than an entered flow's uncertainty."""


@dataclass(frozen=True, slots=True)
class Derived:
    """A point Thermaestro works out from the pump's own values."""

    path: str
    inputs: tuple[int, ...]
    unit: str
    resolution: float
    source: Source
    validity: tuple[str, ...] = ()


def brine_derived(brine_in: int, brine_out: int, pump_speed: int) -> tuple[Derived, ...]:
    """The brine's delta-T and the heat taken from the ground, from these registers."""
    inputs = (brine_in, brine_out, pump_speed)
    return (
        Derived(
            "brine/brine.delta_t",
            inputs,
            "K",
            0.1,
            "calculated",
            ("brine in less brine out; no_flow when the brine pump stands still",),
        ),
        Derived(
            "brine/heat.extracted.power",
            inputs,
            "kW",
            0.01,
            "estimated",
            ("from the entered brine flow, scaled by the brine pump's speed, and the delta-T",),
        ),
        Derived(
            "brine/heat.extracted",
            inputs,
            "kWh",
            0.1,
            "estimated",
            ("the estimated power, added up while Thermaestro runs",),
        ),
    )


BRINE_DERIVED = brine_derived(BRINE_IN, BRINE_OUT, BRINE_PUMP_SPEED)

ADDITION_POINTS = (
    PointDef("addition/power", 43084),
    PointDef("addition/x.nibe.47376", 47376),  # stop of addition, auto mode
    PointDef("addition/x.nibe.47212", 47212),  # most power the internal addition may use
)

LOG_SET = (
    40004, 40008, 40012, 40013, 40014, 40015, 40016, PRIO, COMPRESSOR, 43431, 43433,
    SUPPLY_PUMP_SPEED, BRINE_PUMP_SPEED, 43084, 43005,
)  # fmt: skip
"""The registers worth having pushed: they change fast and the rules need them."""


@dataclass
class Layout:
    """What this model and installation have: nodes and points by path."""

    systems: list[int]
    pools: list[int] = field(default_factory=list)
    nodes: dict[str, str] = field(default_factory=dict)
    """Path below the unit to node kind."""
    points: dict[str, PointDef] = field(default_factory=dict)
    derived: dict[str, Derived] = field(default_factory=dict)
    """Points worked out from the pump's values, where it has their inputs."""


def definitions(
    systems: Iterable[int], pools: Iterable[int] = ()
) -> list[tuple[str | None, str, tuple[PointDef, ...]]]:
    """Thermaestro's points, by node (None for the unit) and node kind, with climate
    systems `systems` and pools `pools`; which of them a pump has depends on its model's
    map."""
    groups: list[tuple[str | None, str, tuple[PointDef, ...]]] = [
        (None, "unit", UNIT_POINTS),
        ("dhw", "dhw_tank", DHW_POINTS),
        ("compressor.ep14", "compressor", COMPRESSOR_POINTS),
        ("brine", "brine_circuit", BRINE_POINTS),
        ("addition", "addition", ADDITION_POINTS),
    ]
    for number in sorted(set(systems)):
        system = SYSTEMS[number - 1]
        system_defs = system_points(system) + (list(CS1_POINTS) if number == 1 else [])
        groups.append((f"cs{number}", "climate_system", tuple(system_defs)))
    for number in sorted(set(pools)):
        groups.append((f"pool{number}", "pool", tuple(pool_points(POOLS[number - 1]))))
    return groups


Groups = Callable[
    [Iterable[int], Iterable[int]], list[tuple[str | None, str, tuple[PointDef, ...]]]
]
"""A family's points by node, with the given climate systems and pools."""
Levers = Callable[
    [ModelMap, Mapping[str, PointDef], Mapping[int, float | int | None]], list["Spec"]
]
"""A family's levers, from the model, the points it has and the values last read."""


@dataclass(frozen=True)
class Family:
    """One family of pumps: the registers the plugin watches itself, and its points."""

    name: str
    """Its register table (`bus`, `s-series`), and its part of a map's name."""
    prio: int
    demand: Mapping[int, str]
    compressor: int
    compressor_running: int
    compressor_changing: Rule
    supply_pump_speed: int
    brine_in: int
    brine_out: int
    brine_pump_speed: int
    brine_out_limit: int
    """The pump's own low brine-out alarm limit."""
    meters: Mapping[int, str]
    """The heat meters, and what they count: production for this demand."""
    systems: tuple[System, ...]
    groups: Groups
    levers: Levers
    lever_paths: tuple[str, ...]
    """Every lever `levers` can offer, below the unit."""
    prio_hot_water: int = PRIO_HOT_WATER
    word_swap: int | None = None
    """The register that sets the word order of 32-bit values; None where it is fixed."""
    firmware: tuple[int, int] | None = None
    """The registers of the firmware's version and release, where the family has them."""
    answers_carry_next: bool = False
    """Whether a read's answer carries the next register's word too, as the bus's does."""
    poll_round_s: float = 0.0
    """The least time a round of polling every point takes. The bus paces reads itself,
    about one a second; a Modbus TCP pump answers at once, so its rounds are spaced."""
    pools: tuple[Pool, ...] = ()
    hot_water_mode: int | None = None
    """The register of the hot-water mode, which the hot-water block follows."""

    @property
    def brine_derived(self) -> tuple[Derived, ...]:
        return brine_derived(self.brine_in, self.brine_out, self.brine_pump_speed)

    @property
    def watched(self) -> tuple[int, ...]:
        """The registers the rules need, whether or not a point shows them."""
        return (self.prio, self.compressor, self.supply_pump_speed, self.brine_pump_speed)

    def layout(self, model: ModelMap, systems: list[int], pools: Iterable[int] = ()) -> Layout:
        """The nodes and points a model has, with climate systems `systems` (1 always) and
        pools `pools`."""
        out = Layout(systems=sorted(set(systems) | {1}), pools=sorted(set(pools)))
        for node, kind, defs in self.groups(out.systems, out.pools):
            present = [p for p in defs if p.register in model]
            if not present:
                continue
            if node is not None:
                out.nodes[node] = kind
            for p in present:
                out.points[p.path] = p
        if "brine" in out.nodes:
            for d in self.brine_derived:
                if all(r in model for r in d.inputs):
                    out.derived[d.path] = d
        return out

    def detectable(self, model: ModelMap) -> list[System]:
        """The climate systems beyond the first that this model can have."""
        return [s for s in self.systems[1:] if s.accessory in model and s.supply in model]

    def detectable_pools(self, model: ModelMap) -> list[Pool]:
        """The pools this model can have."""
        return [p for p in self.pools if p.accessory in model and p.sensor in model]


def unit(register_unit: str) -> str | None:
    return UNITS.get(register_unit, register_unit) or None


LEVERS = (
    "cs1/heating.offset",
    "dhw/mode",
    "dhw/block",
    "dhw/boost_once",
    "alarm.reset",
    "addition/stop_temp",
    "addition/max_power",
)
"""The levers `levers` offers wherever a model's map has their registers, below the unit.
Further climate systems' offsets and the pools' levers come with what is detected."""

HOT_WATER_MODE = 47041
MODES = {"eco": 0, "normal": 1, "lux": 2, "smart": 4}
"""The hot-water mode's values (47041), Smart Control from firmware 8224R1."""
OFFERED_MODES = ("eco", "normal", "lux")
"""What Thermaestro may set. Smart Control is never set while Thermaestro has the lever; a
baseline may be it, and is then put back."""
MODE_TITLES = {0: "Economy", 1: "Normal", 2: "Luxury", 4: "Smart Control"}
MODE_STARTS = {0: 47045, 1: 47044, 2: 47043}
"""Each mode's start temperature. Smart Control has none of its own."""
BLOCK_START = 25.0
"""What the hot-water block lowers the start temperature to: a charge waits until the
charge sensor reaches it, so it is also a floor."""
BOOST = 48132
BOOST_ONCE = 4
"""48132's "one time increase": a charge until the need is met, from firmware 7740R2."""
ALARM_RESET = 45171
ADDITION_STOP = 47376
ADDITION_MAX_POWER = 47212

NEVER_WRITTEN = frozenset(
    {
        47134,  # the hot-water period
        47137,  # the operating mode: the pump stays in auto
        47370,  # allow addition, manual mode only
        47371,  # allow heating, manual mode only
        48852,  # the word order other clients decode by
        *range(47004, 47008),  # the heating curves, systems 4 to 1
        48488, 48489, 47525, 47567,  # systems 5 to 8
        *range(47020, 47027),  # the own curve's points
    }
)  # fmt: skip
"""Registers no lever ever writes."""

DATABASE = "Nibe register database"
SCHEDULE = CompetingFeature(name="the pump's hot-water schedule (menu 2.3)")
"""No register for it is known, so whether it is off is the household's to confirm."""
PRICE_ADAPTION = CompetingFeature(name="the pump's Smart Price Adaption (with myUplink)")
"""It moves the heating offset, the hot-water mode and the pool by price on its own. What
its state register says isn't documented, and a pump doesn't say whether it has myUplink,
so the household confirms it is off."""
PRICE_ADAPTION_STATE = 44908
"""Present in the maps of the models that have Smart Price Adaption."""


@dataclass(frozen=True, slots=True)
class Spec:
    """A lever, and how the plugin carries out what is asked of it."""

    lever: Lever
    register: int | None = None
    """What a setting sets, a trigger writes, or a hold writes while engaged."""
    names: Mapping[str, int] | None = None
    """A setting's values by name, the ones not offered too: a baseline may be one."""
    fire: int | None = None
    cancel: int | None = None
    held: int | None = None
    """A hold on `register`: its value while engaged. Release writes back what was read."""
    starts: Mapping[int, int] | None = None
    """The hot-water block: the start-temperature register of each mode it can block."""

    @property
    def path(self) -> str:
        return self.lever.path


def _number(low: float, high: float, step: float, unit: str | None, basis: str) -> Param:
    return Param(
        type="number",
        unit=unit,
        range=Knowledge(value=Range(min=low, max=high, step=step), known="documented", basis=basis),
    )


def _documented(basis: str | tuple[str, ...]) -> Knowledge[bool]:
    return Knowledge(value=True, known="documented", basis=basis)


STORED = Knowledge[Persistence](
    value=Persistence(kind="stored"), known="documented", basis=DATABASE
)
FLASH = Knowledge[Wear](value=Wear(kind="flash"), known="reported")


def block_how(mode: float | int | None) -> str:
    """What the hot-water block does in the current mode."""
    if mode is None or int(mode) not in MODE_TITLES:
        return f"the current mode's start temperature lowered to {BLOCK_START:.1f} °C"
    if int(mode) not in MODE_STARTS:
        return f"{MODE_TITLES[int(mode)]} has no start temperature of its own: it can't block"
    register = MODE_STARTS[int(mode)]
    return (
        f"{MODE_TITLES[int(mode)]}'s start temperature ({register}) lowered to {BLOCK_START:.1f} °C"
    )


def _setting(
    path: str,
    register: int,
    point: str,
    param: Param,
    works: Knowledge[bool],
    **kw: Any,
) -> Spec:
    names = kw.pop("names", None)
    return Spec(
        Lever(
            path=f"{UNIT}/{path}",
            kind="setting",
            params={"value": param},
            works=works,
            persistence=STORED,
            wear=FLASH,
            verify=Verify(kind="readback", point=f"{UNIT}/{point}"),
            touches=(f"x.nibe.{register}",),
            **kw,
        ),
        register=register,
        names=names,
    )


def levers(
    model: ModelMap, points: Mapping[str, PointDef], values: Mapping[int, float | int | None]
) -> list[Spec]:
    """The levers this pump offers, and how each is carried out. `values` are the values
    last read: the hot-water block's description names the mode it would block."""
    out = []
    priced = (PRICE_ADAPTION,) if PRICE_ADAPTION_STATE in model else ()
    for system in SYSTEMS:
        cs = f"cs{system.number}"
        if system.offset is None or f"{cs}/x.nibe.{system.offset}" not in points:
            continue
        conditions: Knowledge[tuple[str, ...]] = Knowledge()
        if system.room_control is not None and f"{cs}/x.nibe.{system.room_control}" in points:
            conditions = Knowledge(
                value=(f"{UNIT}/{cs}/x.nibe.{system.room_control} == 0",),
                known="documented",
                basis="the pump's room control corrects the supply temperature itself, so it"
                " is off while Thermaestro steers",
            )
        out.append(
            _setting(
                f"{cs}/heating.offset",
                system.offset,
                f"{cs}/x.nibe.{system.offset}",
                _number(-10, 10, 1, None, DATABASE),
                _documented("installer manual"),
                preconditions=conditions,
                competing_features=priced,
            )
        )
    mode_point = f"dhw/x.nibe.{HOT_WATER_MODE}"
    if mode_point in points:
        offered = {name: MODES[name] for name in OFFERED_MODES}
        out.append(
            _setting(
                "dhw/mode",
                HOT_WATER_MODE,
                mode_point,
                Param(
                    type="enum",
                    enum=Knowledge(
                        value=offered,
                        known="documented",
                        basis=(DATABASE, "Smart Control isn't offered"),
                    ),
                ),
                _documented(DATABASE),
                competing_features=(SCHEDULE, *priced),
                names=MODES,
            )
        )
    starts = set(MODE_STARTS.values())
    if mode_point in points and starts <= model.ids and "dhw/temp.charge" in points:
        out.append(
            Spec(
                Lever(
                    path=f"{UNIT}/dhw/block",
                    kind="hold",
                    implementation=Implementation(
                        kind="emulated",
                        how=block_how(values.get(HOT_WATER_MODE)),
                        side_effects=(
                            "a setting write each to engage and release",
                            f"a floor: the pump charges when the charge sensor reaches"
                            f" {BLOCK_START:.1f} °C",
                            "it follows the mode: when the mode changes, it is moved",
                        ),
                    ),
                    works=Knowledge(
                        value=True, known="verified", basis="tested on an F1245, firmware 9721R4"
                    ),
                    persistence=Knowledge(value=Persistence(kind="stored"), known="verified"),
                    wear=FLASH,
                    verify=Verify(
                        kind="effect",
                        point=f"{UNIT}/dhw/temp.charge",
                        expectation="no charge until the charge sensor reaches"
                        f" {BLOCK_START:.1f} °C",
                    ),
                    competing_features=(SCHEDULE, *priced),
                    touches=tuple(f"x.nibe.{r}" for r in sorted(starts)),
                ),
                starts=MODE_STARTS,
            )
        )
    if f"dhw/x.nibe.{BOOST}" in points and "demand" in points:
        out.append(
            Spec(
                Lever(
                    path=f"{UNIT}/dhw/boost_once",
                    kind="trigger",
                    works=_documented("firmware history, 7740R2"),
                    verify=Verify(kind="effect", point=f"{UNIT}/demand", expectation="demand: dhw"),
                    touches=(f"x.nibe.{BOOST}",),
                ),
                register=BOOST,
                fire=BOOST_ONCE,
                cancel=0,
            )
        )
    if ALARM_RESET in model:
        out.append(
            Spec(
                Lever(
                    path=f"{UNIT}/alarm.reset",
                    kind="trigger",
                    works=_documented(f"{DATABASE}: reset alarm by setting value 1"),
                    verify=Verify(kind="none"),
                    touches=(f"x.nibe.{ALARM_RESET}",),
                ),
                register=ALARM_RESET,
                fire=1,
            )
        )
    auto = Knowledge[tuple[str, ...]](
        value=(f"{UNIT}/x.nibe.47137 == 0",),
        known="documented",
        basis="user manual, menu 4.9.2: the addition's stop applies in auto mode",
    )
    if f"addition/x.nibe.{ADDITION_STOP}" in points and "x.nibe.47137" in points:
        out.append(
            _setting(
                "addition/stop_temp",
                ADDITION_STOP,
                f"addition/x.nibe.{ADDITION_STOP}",
                _number(-25, 40, 0.1, "degC", DATABASE),
                _documented(
                    "user manual, menu 4.9.2: above this mean outdoor temperature the"
                    " addition isn't used"
                ),
                preconditions=auto,
            )
        )
    if f"addition/x.nibe.{ADDITION_MAX_POWER}" in points:
        out.append(
            _setting(
                "addition/max_power",
                ADDITION_MAX_POWER,
                f"addition/x.nibe.{ADDITION_MAX_POWER}",
                _number(0, 45, 0.01, "kW", DATABASE),
                _documented(f"{DATABASE}: the internal addition's most power"),
            )
        )
    untried = (DATABASE, "not yet tried on a real pool")
    for pool in POOLS:
        node = f"pool{pool.number}"
        if f"{node}/temp" not in points:
            continue
        for name, register in (("start_temp", pool.start), ("stop_temp", pool.stop)):
            if f"{node}/x.nibe.{register}" in points:
                out.append(
                    _setting(
                        f"{node}/{name}",
                        register,
                        f"{node}/x.nibe.{register}",
                        _number(5, 80, 0.1, "degC", DATABASE),
                        _documented(untried),
                        competing_features=priced,
                    )
                )
        if f"{node}/x.nibe.{pool.activated}" in points:
            out.append(
                Spec(
                    Lever(
                        path=f"{UNIT}/{node}/block",
                        kind="hold",
                        implementation=Implementation(
                            kind="emulated",
                            how=f"pool heating switched off ({pool.activated} = 0)",
                            side_effects=("a setting write each to engage and release",),
                        ),
                        works=_documented(untried),
                        persistence=STORED,
                        wear=FLASH,
                        verify=Verify(
                            kind="effect", point=f"{UNIT}/demand", expectation="no pool demand"
                        ),
                        competing_features=priced,
                        touches=(f"x.nibe.{pool.activated}",),
                    ),
                    register=pool.activated,
                    held=0,
                )
            )
    return out


BUS = Family(
    name="bus",
    prio=PRIO,
    demand=DEMAND,
    compressor=COMPRESSOR,
    compressor_running=COMPRESSOR_RUNNING,
    compressor_changing=compressor_changing,
    supply_pump_speed=SUPPLY_PUMP_SPEED,
    brine_in=BRINE_IN,
    brine_out=BRINE_OUT,
    brine_pump_speed=BRINE_PUMP_SPEED,
    brine_out_limit=BRINE_OUT_LIMIT,
    meters=METERS,
    systems=SYSTEMS,
    groups=definitions,
    levers=levers,
    lever_paths=LEVERS,
    word_swap=WORD_SWAP,
    firmware=FIRMWARE,
    answers_carry_next=True,
    pools=POOLS,
    hot_water_mode=HOT_WATER_MODE,
)
"""The bus family: F-series, VVM, SMO and MHB, through a gateway on the MODBUS40 bus."""
