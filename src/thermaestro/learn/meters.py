"""Which energy counter meters which pump's supply, from the sensors set up."""

from ..core.sensors import SENSORS
from ..core.values import Key
from ..store import Database, Sensor


async def pump_meters(db: Database) -> dict[str, Key]:
    """By pump (its unit, `pump:hp1`): its meter's point, in kWh."""
    found = await db.all(Sensor)
    return {
        s.meters: Key(SENSORS, f"{id}/energy")
        for id, s in sorted(found.items())
        if s.meters and s.quantity == "energy"
    }
