"""What the house has and shows, as intents need it: the climate systems, rooms, tanks and
pools the plugins describe and what can be changed in each; and for seeding, how the
house has run over the last week. A climate system's emitter is setup's answer only.
"""

import logging
import statistics
from datetime import UTC, date, timedelta, tzinfo
from zoneinfo import ZoneInfo

from .. import clock
from ..intents import Capabilities, Found
from ..store import Database, Home, Location, Room, Sensor
from .host import PluginHost, State
from .values import Key, Values

log = logging.getLogger(__name__)

WEEK = timedelta(days=7)
DAY = timedelta(days=1)
EDGES = ("start", "stop")


class House:
    def __init__(self, db: Database, host: PluginHost, values: Values) -> None:
        self._db = db
        self._host = host
        self._values = values
        self.capabilities = Capabilities()

    def _nodes(self) -> list[tuple[str, str, str]]:
        """(instance, node path, kind) of every node of every plugin that is up."""
        out: list[tuple[str, str, str]] = []
        for instance in self._host.instances.values():
            if instance.state is State.UP and instance.described is not None:
                out.extend((instance.id, n.path, n.kind) for n in instance.described.nodes)
        return out

    async def refresh(self) -> Capabilities:
        nodes = self._nodes()
        scope = {(i, p): f"{i}:{p}" for i, p, _ in nodes}
        usable: set[str] = set()
        prices = False
        for instance in self._host.instances.values():
            if instance.state is not State.UP or instance.described is None:
                continue
            usable.update(
                f"{instance.id}:{lv.path}"
                for lv in instance.described.levers
                if lv.unavailable is None
            )
            prices = prices or any(s.kind == "price" for s in instance.described.series)
        systems = {scope[(i, p)] for i, p, k in nodes if k == "climate_system"}
        tanks = {scope[(i, p)] for i, p, k in nodes if k == "dhw_tank"}
        pools = {scope[(i, p)] for i, p, k in nodes if k == "pool"}
        home = await self._db.get(Home) or Home()
        self.capabilities = Capabilities(
            systems=frozenset(systems),
            rooms=await self._rooms(systems),
            offset=frozenset(s for s in systems if f"{s}/heating.offset" in usable),
            tanks=frozenset(tanks),
            block=frozenset(t for t in tanks if f"{t}/block" in usable),
            pools=frozenset(p for p in pools if any(u.startswith(f"{p}/") for u in usable)),
            addition=any(
                any(u.startswith(f"{i}:{p}/") for u in usable)
                for i, p, k in nodes
                if k == "addition"
            ),
            power=any(k.point.endswith("grid.import.power") for k in self._values.latest),
            prices=prices,
            emitters=dict(home.emitters),
        )
        return self.capabilities

    async def _rooms(self, systems: set[str]) -> dict[str, str]:
        """Rooms with a temperature sensor, to their climate system."""
        rooms = await self._db.all(Room)
        sensed = {
            s.room
            for s in (await self._db.all(Sensor)).values()
            if s.room is not None and s.quantity == "temperature"
        }
        return {
            f"room:{id}": room.climate_system
            for id, room in rooms.items()
            if id in sensed and room.climate_system in systems
        }

    async def found(self) -> Found:
        """How the house has run over the last week, for seeding. A climate system whose
        rooms have no day of readings yet is left out until they have one. A tank's or
        pool's start and stop temperatures where its device says them, else its usual
        daily low and high."""
        caps = self.capabilities
        now = clock.time()
        location = await self._db.get(Location)
        zone = ZoneInfo(location.timezone) if location is not None else UTC
        means = {}
        for room in caps.rooms:
            days = await self._week(
                Key("site", f"room.{room.removeprefix('room:')}/temperature"), now, zone
            )
            if days:
                means[room] = statistics.fmean(mean for _, _, mean, _ in days)
        waiting = {s for r, s in caps.rooms.items() if r not in means}
        tanks = await self._ranges(caps.tanks, "temp.top", now, zone) | self._settings(caps.tanks)
        pools = await self._ranges(caps.pools, "temp", now, zone) | self._settings(caps.pools)
        return Found(
            systems=tuple(sorted(caps.systems - waiting)),
            rooms=dict(caps.rooms),
            room_means=means,
            tanks=tanks,
            pools=pools,
            addition=caps.addition,
        )

    def _settings(self, scopes: frozenset[str]) -> dict[str, tuple[float, float]]:
        """For each node whose device says them, its start and stop temperatures."""
        out = {}
        for scope in scopes:
            instance, _, path = scope.partition(":")
            found = [self._values.latest.get(Key(instance, f"{path}/temp.{e}")) for e in EDGES]
            values = [
                e.value
                for e in found
                if e is not None and e.quality == "good" and isinstance(e.value, float)
            ]
            if len(values) == 2:
                out[scope] = (values[0], values[1])
        return out

    async def _ranges(
        self, scopes: frozenset[str], point: str, now: float, zone: tzinfo
    ) -> dict[str, tuple[float, float]]:
        """For each node, the median of its point's daily lows and of its daily highs."""
        out = {}
        for scope in scopes:
            instance, _, path = scope.partition(":")
            days = await self._week(Key(instance, f"{path}/{point}"), now, zone)
            if days:
                out[scope] = (
                    statistics.median(low for _, low, _, _ in days),
                    statistics.median(high for _, _, _, high in days),
                )
        return out

    async def _week(
        self, key: Key, now: float, zone: tzinfo
    ) -> list[tuple[date, float, float, float]]:
        """The point's days over the last week, once its history goes back a day."""
        first = await self._values.first(key)
        if first is None or first > now - DAY.total_seconds():
            return []
        return await self._values.daily(key, now - WEEK.total_seconds(), now, zone)
