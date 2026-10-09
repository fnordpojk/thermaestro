"""The S-series pump profile, over the pump's own Modbus TCP server.

From Nibe's S-series Modbus document (TIF SV 2608) and the S-series register table; no
register here has been read on a real S-series pump yet. Registers are numbered as the
table does: input register n is 3nnnn, holding register n is 4nnnn.

The S-series reports what the bus family leaves to be inferred: the reversing valve's
position (QN10) is a register of its own, and its heat meters are documented for every
model. The compressor's status is only off or on. Its levers are described and
unavailable until they have been tried on a real pump.
"""

from collections.abc import Iterable, Mapping

from .. import durations
from ..cap.model import Lever, Verify
from .maps import ModelMap
from .profile import (
    BRINE_STOPPED,
    METER_IDLE_S,
    SUPPLY_STOPPED,
    UNIT,
    Family,
    PointDef,
    Spec,
    System,
    changing,
    charge_starting,
    counting,
    diverted,
    no_flow,
)

PRIO = 31028
COMPRESSOR = 31100
SUPPLY_PUMP_SPEED = 31102
BRINE_PUMP_SPEED = 31104
DIVERTER = 32196
BRINE_IN, BRINE_OUT = 30010, 30011
BRINE_OUT_LIMIT = 40190
"""The pump's own low brine-out alarm limit (EP14)."""

DEMAND = {10: "idle", 20: "dhw", 30: "heating", 40: "pool", 60: "cooling"}
COMPRESSOR_STATE = {0: "stopped", 1: "running"}
DIVERTER_POSITION = {0: "heating", 1: "dhw"}

METERS = {31575: "dhw", 31577: "heating", 31581: "pool", 31583: "dhw", 31585: "heating"}

SYSTEMS = (System(1, 30005, None, offset=40030),)
"""Climate system 1 only, for now: how the S-series shows further systems isn't read yet."""

compressor_changing = changing(COMPRESSOR, COMPRESSOR_STATE, frozenset())
"""The status is off or on, so nothing is transitional by it."""
FLOW_RULES = (no_flow(SUPPLY_PUMP_SPEED, SUPPLY_STOPPED),)
BRINE_RULES = (no_flow(BRINE_PUMP_SPEED, BRINE_STOPPED),)

UNIT_POINTS = (
    PointDef("outdoor.temp", 30001),
    PointDef("demand", PRIO, enum=DEMAND),
    PointDef("diverter", DIVERTER, enum=DIVERTER_POSITION),
    PointDef("degree_minutes", 40011),
    PointDef("alarm", 31975),
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
            ("heat.produced{purpose=dhw,by=total}", 31575),
            ("heat.produced{purpose=heating,by=total}", 31577),
            ("heat.produced{purpose=pool,by=compressor}", 31581),
            ("heat.produced{purpose=dhw,by=compressor}", 31583),
            ("heat.produced{purpose=heating,by=compressor}", 31585),
        )
    ),
    # The table's factor for the power used is 1 (W); the Modbus document's is 10. Shown
    # as the pump's own until a real pump settles it.
    PointDef("x.nibe.32166", 32166),
    PointDef("x.nibe.40237", 40237),  # operating mode
    PointDef("x.nibe.40180", 40180),  # addition allowed
    PointDef("x.nibe.40181", 40181),  # heating allowed
)

CS1_POINTS = (
    PointDef(
        "cs1/supply.temp",
        30005,
        rules=(*FLOW_RULES, diverted(DIVERTER, 1)),
        validity=(
            f"no_flow when supply pump {SUPPLY_PUMP_SPEED} is 0",
            "during a hot-water charge it describes the charge, not the heating",
        ),
    ),
    PointDef(
        "cs1/return.temp",
        30007,
        rules=FLOW_RULES,
        validity=(f"no_flow when supply pump {SUPPLY_PUMP_SPEED} is 0",),
    ),
    PointDef("cs1/room.temp", 30026),
    PointDef("cs1/pump.speed", SUPPLY_PUMP_SPEED),
    PointDef("cs1/x.nibe.31017", 31017),  # calculated supply
    PointDef("cs1/x.nibe.30039", 30039),  # external supply (BT25)
    PointDef("cs1/x.nibe.30040", 30040),  # flow (BF1)
    PointDef("cs1/x.nibe.40026", 40026),  # heating curve
    PointDef("cs1/x.nibe.40030", 40030),  # heating offset
)

DHW_POINTS = (
    PointDef("dhw/temp.top", 30008),
    PointDef(
        "dhw/temp.charge",
        30009,
        rules=(charge_starting,),
        validity=("transitional for a while after a charge starts: tank-bottom water passes it",),
    ),
    PointDef("dhw/x.nibe.32014", 32014),  # hot water start (BT5)
    PointDef("dhw/x.nibe.40056", 40056),  # hot water demand mode
)

COMPRESSOR_POINTS = (
    PointDef("compressor.ep14/state", COMPRESSOR, enum=COMPRESSOR_STATE),
    PointDef("compressor.ep14/speed", 31046),
    PointDef("compressor.ep14/x.nibe.31083", 31083),  # starts
)

BRINE_POINTS = (
    PointDef(
        "brine/brine.in.temp",
        BRINE_IN,
        rules=BRINE_RULES,
        validity=(f"no_flow when brine pump {BRINE_PUMP_SPEED} is 0",),
    ),
    PointDef(
        "brine/brine.out.temp",
        BRINE_OUT,
        rules=BRINE_RULES,
        validity=(f"no_flow when brine pump {BRINE_PUMP_SPEED} is 0",),
    ),
    PointDef("brine/pump.speed", BRINE_PUMP_SPEED),
    PointDef(f"brine/x.nibe.{BRINE_OUT_LIMIT}", BRINE_OUT_LIMIT),
)


def definitions(
    systems: Iterable[int], pools: Iterable[int] = ()
) -> list[tuple[str | None, str, tuple[PointDef, ...]]]:
    """The S-series points by node; climate system 1 only, and no pools yet."""
    return [
        (None, "unit", UNIT_POINTS),
        ("dhw", "dhw_tank", DHW_POINTS),
        ("compressor.ep14", "compressor", COMPRESSOR_POINTS),
        ("brine", "brine_circuit", BRINE_POINTS),
        ("cs1", "climate_system", CS1_POINTS),
    ]


UNAVAILABLE = "not yet read on a real S-series pump"


def levers(
    model: ModelMap, points: Mapping[str, PointDef], values: Mapping[int, float | int | None]
) -> list[Spec]:
    """Described, and unavailable: what the S-series takes is to be read on a real pump
    first."""
    out = []
    for path, register in (("cs1/heating.offset", 40030), ("dhw/mode", 40056)):
        point = f"{path.split('/')[0]}/x.nibe.{register}"
        if point in points:
            lever = Lever(
                path=f"{UNIT}/{path}",
                kind="setting",
                verify=Verify(kind="readback", point=f"{UNIT}/{point}"),
                touches=(f"x.nibe.{register}",),
                unavailable=UNAVAILABLE,
            )
            out.append(Spec(lever, register=register))
    return out


S_SERIES = Family(
    name="s-series",
    prio=PRIO,
    demand=DEMAND,
    compressor=COMPRESSOR,
    compressor_running=1,
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
    lever_paths=("cs1/heating.offset", "dhw/mode"),
    poll_round_s=10.0,
)
"""The S-series: S1155, S1255, S2125, S320, SMO S40, VVM S320 and their kin, over Modbus TCP."""
