"""What the house shows that a person may want to know, looked at every round and kept as
events (`core.events`):
- a protection floor broken: a tank's top below its floor;
- a climate system none of whose rooms has a fresh temperature, so its offset is the
  pump's own meanwhile;
- a setting let go after a change Thermaestro didn't make;
- a plugin, a device or a feed of prices or weather, missing for over an hour;
- a device's own warnings, as long as the device says they are on;
- a room below its band for over half an hour (the tolerated shortfall), once its heat has
  had time to show since the band last rose.

What an event says holds still while its condition lasts (the floor, not the temperature
of the minute), so it is told once, and again only when it really changes.
"""

from collections.abc import Mapping

from .. import clock
from ..core.events import Events, Raise, mark
from ..core.executor import Executor
from ..core.host import PluginHost, State
from . import rules
from .model import Situation

JUMP_S = 60.0
"""A jump of the system clock beyond this is set right in the timers."""
MISSING_S = 3600.0
"""How long a plugin may be down, or a series late, before it is an event."""
MISSED_S = 1800.0
"""How long a room may be below its band before it is missed."""
BELOW = 0.1
"""°C below its band's low edge a room must be to count as below it."""

FLOOR_BROKEN = mark("The hot water fell below its floor of %(floor)s °C")
NO_FRESH_ROOM = mark(
    "No room in %(system)s has a fresh temperature: its offset is the pump's own meanwhile"
)
LET_GO = mark("%(lever)s was let go: %(why)s")
DOWN = mark("%(name)s has been %(state_label)s for over an hour")
UNHEALTHY = mark("%(name)s has been down for over an hour")
ASKS = mark("%(name)s needs you: %(why)s")
LATE = mark("%(name)s hasn't published %(series)s for over an hour")
WARNING = mark("%(name)s: %(text)s")
COMFORT_MISSED = mark("%(room)s has been below its %(low)s °C for over half an hour")


class Watch:
    def __init__(self, events: Events, executor: Executor, host: PluginHost) -> None:
        self._events = events
        self._executor = executor
        self._host = host
        self._since: dict[str, float] = {}
        """When each condition of a plugin was first seen, by its key."""
        self._low: dict[str, float | None] = {}
        """Each room's band's low edge as the last round saw it."""
        self._rose: dict[str, float] = {}
        """When each room's band last rose, or was first seen."""
        self._below: dict[str, float] = {}
        self._last: tuple[float, float] | None = None
        """The wall clock and the steady one at the last look."""
        """Since when each room has been below its band."""

    def state(self) -> dict[str, dict[str, float | None]]:
        """The timers, to keep with the planner's memory."""
        return {
            "since": dict(self._since),
            "low": dict(self._low),
            "rose": dict(self._rose),
            "below": dict(self._below),
        }

    def load(self, state: Mapping[str, Mapping[str, float | None]], *, fresh: bool) -> None:
        """The timers as kept: when bands rose always; how long a condition has held only
        if they were kept `fresh`, since a stop leaves it unknown."""
        self._low = dict(state.get("low", {}))
        self._rose = {k: float(v) for k, v in state.get("rose", {}).items() if v is not None}
        if fresh:
            self._since = {k: float(v) for k, v in state.get("since", {}).items() if v is not None}
            self._below = {k: float(v) for k, v in state.get("below", {}).items() if v is not None}

    async def resume(self) -> None:
        """Conditions told before a restart and still open stay open while they still
        hold: their timers go on from when they were raised."""
        for event in await self._events.open("plugin.missing:"):
            self._since.setdefault(event.key, event.raised - MISSING_S)
        for event in await self._events.open("comfort.missed:"):
            self._below.setdefault(event.scope, event.raised - MISSED_S)

    async def look(self, sit: Situation) -> None:
        now = sit.now.timestamp()
        self._steady()
        await self._events.sync("floor.broken:", self._floors(sit), now=now)
        await self._events.sync("room.stale:", self._rooms(sit), now=now)
        await self._events.sync("lever.let_go:", self._let_go(), now=now)
        await self._events.sync("plugin.missing:", self._plugins(now), now=now)
        await self._events.sync("device.warning:", self._warnings(), now=now)
        await self._events.sync("comfort.missed:", self._comfort(sit, now), now=now)

    def _steady(self) -> None:
        """The system clock set right (by NTP, say) moves the timers with it, so the jump
        isn't taken for time passed: the wall clock against one that only goes forward."""
        wall, steady = clock.time(), clock.monotonic()
        if self._last is not None:
            jump = (wall - self._last[0]) - (steady - self._last[1])
            if abs(jump) > JUMP_S:
                for timers in (self._since, self._rose, self._below):
                    for key in timers:
                        timers[key] += jump
        self._last = (wall, steady)

    def _floors(self, sit: Situation) -> dict[str, Raise]:
        out = {}
        for scope, tank in sit.tanks.items():
            floor = sit.force.bound("tank_top_temp", scope)
            if floor is None or floor.low is None or tank.top is None:
                continue
            if tank.top < floor.low:
                args = {"floor": round(floor.low, 1)}
                out[f"floor.broken:{scope}"] = Raise("floor.broken", scope, FLOOR_BROKEN, args)
        return out

    def _rooms(self, sit: Situation) -> dict[str, Raise]:
        by_system: dict[str, list[float]] = {}
        for room in sit.rooms:
            by_system.setdefault(room.system, []).append(room.weight)
        out = {}
        for system, weights in sorted(by_system.items()):
            if weights and all(w <= 0 for w in weights):
                args = {"system": system}
                out[f"room.stale:{system}"] = Raise("room.stale", system, NO_FRESH_ROOM, args)
        return out

    def _let_go(self) -> dict[str, Raise]:
        out = {}
        for ref, claim in sorted(self._executor.claims.items()):
            if claim.drift is not None:
                args = {"lever": ref, "why": claim.drift}
                out[f"lever.let_go:{ref}"] = Raise("lever.let_go", ref, LET_GO, args)
        return out

    def _plugins(self, now: float) -> dict[str, Raise]:
        seen: dict[str, Raise] = {}
        for id, instance in sorted(self._host.instances.items()):
            name = {"name": id}
            health = instance.health
            if instance.state != State.UP:
                seen[f"plugin.missing:{id}"] = Raise(
                    "plugin.missing", id, DOWN, {**name, "state_label": str(instance.state)}
                )
            elif health is not None and health.needs_user_action:
                seen[f"plugin.missing:{id}"] = Raise(
                    "plugin.missing", id, ASKS, {**name, "why": health.needs_user_action}
                )
            elif health is not None and health.state == "down":
                seen[f"plugin.missing:{id}"] = Raise("plugin.missing", id, UNHEALTHY, name)
            elif health is not None and health.stale:
                series = ", ".join(sorted(health.stale))
                seen[f"plugin.missing:{id}"] = Raise(
                    "plugin.missing", id, LATE, {**name, "series": series}
                )
        for key in [k for k in self._since if k not in seen]:
            del self._since[key]
        out = {}
        for key, what in seen.items():
            since = self._since.setdefault(key, now)
            if now - since >= MISSING_S:
                out[key] = what
        return out

    def _comfort(self, sit: Situation, now: float) -> dict[str, Raise]:
        out = {}
        for room in sit.rooms:
            bound = sit.force.bound("room_temp", room.scope) or sit.force.bound(
                "room_temp", room.system
            )
            low = bound.low if bound is not None else None
            before = self._low.get(room.scope, None)
            if room.scope not in self._low or (
                low is not None and (before is None or low > before + 1e-9)
            ):
                self._rose[room.scope] = now
            self._low[room.scope] = low
            reach_s = rules._response_h(sit, room.system) * 3600
            below = (
                low is not None
                and room.temp is not None
                and room.weight > 0
                and not room.airing
                and room.temp < low - BELOW
                and now - self._rose[room.scope] >= reach_s
            )
            if not below or low is None:
                self._below.pop(room.scope, None)
                continue
            since = self._below.setdefault(room.scope, now)
            if now - since >= MISSED_S:
                args = {"room": room.name or rules._name(room.scope), "low": round(low, 1)}
                out[f"comfort.missed:{room.scope}"] = Raise(
                    "comfort.missed", room.scope, COMFORT_MISSED, args, audience="household"
                )
        return out

    def _warnings(self) -> dict[str, Raise]:
        out = {}
        for id, instance in sorted(self._host.instances.items()):
            for (unit, code), event in sorted(instance.active.items()):
                scope = f"{id}:{unit}" if unit else id
                args = {"name": id, "text": event.text}
                out[f"device.warning:{scope}:{code}"] = Raise(
                    "device.warning", scope, WARNING, args
                )
        return out
