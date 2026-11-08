"""Whether anyone is home: concluded from the evidence, shown, and acted on by nothing yet.

The evidence, each from its source:
- **an alarm** (a sensor measuring `alarm_panel`): armed for away or vacation says nobody is
  home, at once; armed for the night or at home says someone is; a disarm or a trigger is
  someone there;
- **presence, occupancy and motion sensors**: one on is activity, at that moment;
- **hot water drawn**: found in the tank's readings as the daily learner finds draws, a few
  minutes after.

No evidence is not evidence of nobody. Each source is known while its readings are good; a
house is concluded empty from quiet only after every source it has stayed known and quiet
for `QUIET_S` (a house with a single source, `QUIET_ONE_S`), the household's quiet hours not
counted. Home is concluded from a draw, a disarm, or activity spread over `SPREAD_S`. A
short gap in what is known of an armed alarm doesn't end the absence it showed, if it is
still armed for away once known again and nothing showed activity meanwhile. The time
Thermaestro is down is not known, so a quiet window starts again after a restart.

Each conclusion is kept with when what it concluded began and when it was concluded, so a
later look at the past uses only what was known then. Nothing plans with it yet.
"""

import asyncio
import contextlib
import json
import logging
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal

from .cap.model import Envelope
from .cap.vocabulary import ALARM_PANEL
from .core.events import mark
from .core.failures import failures
from .core.sensors import SENSORS, SensorHub
from .core.values import Key, Values
from .store import Database

if TYPE_CHECKING:
    from .intents import Intents
    from .learn import Learnings
    from .learn.hotwater import HotWater, TankKeys

log = logging.getLogger(__name__)

TICK_S = 60.0
"""How often the evidence is weighed."""
GAP_S = 3 * TICK_S
"""Longer between two weighings is a gap: Thermaestro, or its loop, was away."""
DRAWS_EVERY_S = 300.0
"""How often the tank's readings are searched for draws."""
DRAWS_LOOK_S = 2 * 86_400.0
"""How far back the tank's quiet noise and standing loss are measured."""
MEASURE_EVERY_S = 6 * 3600.0
DRAWS_SCAN_S = 6 * 3600.0
"""How far back each search for draws reads: a draw takes well under this."""
QUIET_S = 12 * 3600.0
QUIET_ONE_S = 24 * 3600.0
SPREAD_S = 30 * 60.0
"""Activity spread over this long is someone home, not a moment."""
ACTIVITY_KEEP_S = 2 * 3600.0
"""Activity is kept this long, to see whether it spreads."""

PRESENCE = frozenset({"presence", "occupancy", "motion"})
AWAY_ARMED = frozenset({"armed_away", "armed_vacation"})
HOME_ARMED = frozenset({"armed_home", "armed_night"})
SOMEONE = frozenset({"disarmed", "triggered", "pending", "disarming"})
"""Alarm states that say someone is there: a disarm, or the alarm going off."""

WEIGHING = mark("Weighing whether anyone is home")

State = Literal["home", "away", "unknown"]
Source = Literal["alarm", "presence", "draws"]


@dataclass(frozen=True)
class Conclusion:
    state: State
    concluded: float
    """When it was concluded."""
    began: float | None
    """When what it concludes began, by the evidence."""
    why: str
    """`alarm`, `quiet`, `alarm_home`, `disarmed`, `draw`, `activity`, `start`, `no_sources`."""
    args: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Activity:
    t: float
    kind: Literal["presence", "alarm", "draw"]


@dataclass(frozen=True)
class SourceState:
    known: bool
    why: str | None = None


class Occupancy:
    def __init__(
        self,
        db: Database,
        values: Values,
        sensors: SensorHub,
        *,
        hot_water: "HotWater | None" = None,
        usable: Callable[[Key], bool] | None = None,
        learnings: "Learnings | None" = None,
        intents: "Intents | None" = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._db = db
        self._values = values
        self._sensors = sensors
        self._hot_water = hot_water
        self._usable = usable
        self._learnings = learnings
        self._intents = intents
        self._clock = clock
        self.now: Conclusion = Conclusion("unknown", clock(), None, "start")
        self._started = clock()
        self._last_tick: float | None = None
        self._quiet_s = 0.0
        """Quiet time counted in the current window, the household's quiet hours left out."""
        self._last_activity: float | None = None
        self._activity: list[float] = []
        self._pending: list[Activity] = []
        """Activity heard since it was last weighed."""
        self._seen_since: float | None = None
        """During an absence: the latest activity, not yet enough to conclude a return."""
        self._quiet_slot: tuple[float, bool] | None = None
        self._measured: dict[str, tuple[float, float, float]] = {}
        """Per tank: when measured, its standing loss and what opens a draw."""
        self._draws_at = 0.0
        self._draws_state = SourceState(False, "not searched yet")
        self._last_draw: dict[str, float] = {}
        self._presence_at: dict[str, float] = {}
        """Per presence sensor, when it was last seen turning on."""
        self._word: str | None = None
        """The alarm's state when last known: a change from it is a change, one sent again
        isn't."""
        self.sources: dict[str, SourceState] = {}
        self._task: asyncio.Task[None] | None = None
        values.listeners.append(self.heard)

    # --- starting and stopping -------------------------------------------------------------

    async def load(self) -> None:
        """The last conclusion, as it was before a restart; the quiet window starts again."""

        def read(t: Any) -> tuple[Any, ...] | None:
            row: tuple[Any, ...] | None = t.execute(
                "SELECT concluded, began, state, why, args FROM occupancy ORDER BY id DESC LIMIT 1"
            ).fetchone()
            return row

        found = await self._db.run(read)
        if found is not None:
            concluded, began, state, why, args = found
            self.now = Conclusion(state, concluded, began, why, json.loads(args))

    def start(self) -> None:
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        with contextlib.suppress(ValueError):
            self._values.listeners.remove(self.heard)

    async def _run(self) -> None:
        while True:
            try:
                await self.tick()
            except Exception as e:  # noqa: BLE001 - logged and raised as an event
                failures.failed("occupancy", WEIGHING, e, logger=log)
            else:
                failures.worked("occupancy")
            await asyncio.sleep(TICK_S)

    # --- the evidence ----------------------------------------------------------------------

    def _keys(self, quantities: Iterable[str]) -> list[Key]:
        wanted = set(quantities)
        return [
            Key(SENSORS, f"{id}/{s.quantity}")
            for id, s in sorted(self._sensors.sensors.items())
            if s.quantity in wanted
        ]

    def heard(self, instance: str, envelope: Envelope) -> None:
        """Presence turning on, as it comes: a sensor may be on for only a moment between
        two weighings. One remembered from before a restart isn't new, nor is one sent again
        unchanged (Home Assistant back, sending every state) with the time it was seen."""
        if instance != SENSORS:
            return
        try:
            t = (envelope.t_observed or envelope.t_received).timestamp()
            if t < self._started or envelope.quality != "good":
                return
            quantity = envelope.point.rpartition("/")[2]
            seen = self._presence_at.get(envelope.point, float("-inf"))
            if quantity in PRESENCE and envelope.value is True and t > seen:
                self._presence_at[envelope.point] = t
                self._pending.append(Activity(t, "presence"))
        except Exception as e:  # noqa: BLE001 - logged and raised as an event, never into a sensor
            failures.failed("occupancy", WEIGHING, e, logger=log)

    def _alarm(self) -> tuple[str | None, float | None, SourceState]:
        """The alarm's state word, since when, and whether it is known. With several alarms,
        armed for away only where every one is."""
        keys = self._keys([ALARM_PANEL])
        if not keys:
            return None, None, SourceState(True)  # no alarm: not a source at all
        words, times = [], []
        for key in keys:
            found = self._values.latest.get(key)
            if found is None or found.quality != "good" or not isinstance(found.value, str):
                why = found.why if found is not None and found.why else "no value yet"
                return None, None, SourceState(False, why)
            words.append(found.value)
            times.append((found.t_observed or found.t_received).timestamp())
        if all(w in AWAY_ARMED for w in words):
            return words[0], max(times), SourceState(True)
        for w, t in zip(words, times, strict=True):
            if w not in AWAY_ARMED:
                return w, t, SourceState(True)
        return None, None, SourceState(True)

    def _presence(self) -> SourceState:
        for key in self._keys(PRESENCE):
            found = self._values.latest.get(key)
            if found is None or found.quality != "good":
                return SourceState(False, found.why if found is not None else "no value yet")
        return SourceState(True)

    async def _draws(self, now: float) -> SourceState:
        """New draws from the tank's readings, as activity; whether the tank is read well
        enough to find them."""

        if self._hot_water is None:
            return SourceState(False, "no tank")
        if now - self._draws_at < DRAWS_EVERY_S:
            return self._draws_state
        self._draws_at = now
        self._draws_state = await self._search(now)
        return self._draws_state

    async def _search(self, now: float) -> SourceState:
        """Search each tank's readings for draws since the last search; whether they are
        read well enough to find them: usable, and read within the last hour, without a
        pause over an hour in the last two (as the history holds them, so a replay judges
        them as it was then)."""
        from .learn import tank

        assert self._hot_water is not None  # noqa: S101 - checked by the caller
        tanks = list(self._hot_water.tanks())
        for keys in tanks:
            for key in (keys.top, keys.low, keys.demand):
                if self._usable is not None and not self._usable(key):
                    return SourceState(False, "the tank's readings aren't usable yet")
        for keys in tanks:
            k, opens = await self._measure(keys, now)
            since = now - DRAWS_SCAN_S
            top = tank.Series(await self._values.history(keys.top, since, now))
            low = tank.Series(await self._values.history(keys.low, since, now))
            for series in (top, low):
                if not tank.covers(series.samples, now - 2 * 3600, now):
                    return SourceState(False, "the tank isn't read")
            demand = await self._values.history(keys.demand, since - 3600, now)
            charges = tank.charges(demand, now, top)
            quarters = tank.steps(top, low, charges, since, now)
            last = self._last_draw.get(keys.scope, self._started)
            for draw in tank.draws(quarters, k, opens):
                if draw.at > last:
                    self._pending.append(Activity(draw.at, "draw"))
                    self._last_draw[keys.scope] = max(self._last_draw.get(keys.scope, 0), draw.at)
        return SourceState(True)

    async def _measure(self, keys: "TankKeys", now: float) -> tuple[float, float]:
        """The tank's standing loss and what opens a draw, from its last two days, as the
        daily learner finds them: again every `MEASURE_EVERY_S`, as they change slowly."""
        from .learn import tank
        from .learn.hotwater import LOSS

        kept = self._measured.get(keys.scope)
        if kept is not None and now - kept[0] < MEASURE_EVERY_S:
            return kept[1], kept[2]
        since = now - DRAWS_LOOK_S
        top = tank.Series(await self._values.history(keys.top, since, now))
        low = tank.Series(await self._values.history(keys.low, since, now))
        demand = await self._values.history(keys.demand, since - 3600, now)
        charges = tank.charges(demand, now, top)
        learned = self._learnings.used(LOSS, keys.scope) if self._learnings else None
        k = float(learned) if isinstance(learned, int | float) else None
        if k is None:
            k = tank.loss_rate(tank.steps(top, low, charges, since, now, step=3600)) or 0.0
        opens = tank.threshold(tank.steps(top, low, charges, since, now), k)
        self._measured[keys.scope] = (now, k, opens)
        return k, opens

    async def _quiet_hours(self, now: float) -> bool:
        """Whether the household's quiet hours are in force: asked once a quarter hour."""
        if self._intents is None:
            return False
        slot = now // 900
        if self._quiet_slot is not None and self._quiet_slot[0] == slot:
            return self._quiet_slot[1]
        force = await self._intents.in_force(datetime.fromtimestamp(now, UTC))
        bound = force.bound("quiet", "house")
        quiet = bound is not None and bound.value is True
        self._quiet_slot = (slot, quiet)
        return quiet

    # --- weighing --------------------------------------------------------------------------

    async def tick(self, now: float | None = None) -> Conclusion:
        now = self._clock() if now is None else now
        dt = 0.0 if self._last_tick is None else now - self._last_tick
        gap = self._last_tick is None or dt > GAP_S
        self._last_tick = now
        word, armed_t, alarm = self._alarm()
        has_alarm = bool(self._keys([ALARM_PANEL]))
        has_presence = bool(self._keys(PRESENCE))
        has_tank = self._hot_water is not None and any(True for _ in self._hot_water.tanks())
        self.sources = {}
        if has_alarm:
            self.sources["alarm"] = alarm
        if has_presence:
            self.sources["presence"] = self._presence()
        if has_tank:
            self.sources["draws"] = await self._draws(now)
        if alarm.known and word is not None:
            if self._word is not None and word != self._word and word in SOMEONE:
                self._pending.append(Activity(armed_t or now, "alarm"))  # disarmed, or set off
            self._word = word
        events, self._pending = sorted(self._pending, key=lambda a: a.t), []
        if not self.sources:
            return await self._conclude("unknown", None, "no_sources", now)

        # An alarm says it outright.
        if alarm.known and word in AWAY_ARMED:
            self._reset(now)
            if not (self.now.state == "away" and self.now.why == "alarm"):
                return await self._conclude("away", armed_t, "alarm", now)
            return self.now
        if alarm.known and word in HOME_ARMED:
            self._reset(now)
            return await self._conclude("home", armed_t, "alarm_home", now)
        # A gap in what is known of the alarm leaves its absence as it is, unless something
        # shows activity meanwhile (a draw) or the alarm is found disarmed once known again.
        by_alarm = self.now.state == "away" and self.now.why == "alarm"
        disarms = [e for e in events if e.kind == "alarm"]
        if disarms or (by_alarm and alarm.known and word in SOMEONE):
            at = disarms[-1].t if disarms else armed_t
            self._reset(now)
            return await self._conclude("home", at, "disarmed", now, at=at)

        # Activity: someone, for a moment or for good.
        if events:
            self._quiet_s = 0.0
            self._last_activity = events[-1].t
            self._activity = [t for t in self._activity if t > now - ACTIVITY_KEEP_S]
            self._activity += [e.t for e in events]
            draws = [e for e in events if e.kind == "draw"]
            if draws:
                self._activity = []
                return await self._conclude("home", draws[-1].t, "draw", now, at=draws[-1].t)
            if self._activity[-1] - self._activity[0] >= SPREAD_S:
                began = self._activity[0]
                self._activity = []
                return await self._conclude("home", began, "activity", now)
            if self.now.state == "away":
                self._seen_since = events[-1].t
            return self.now

        # Quiet: counted while everything is known, the household's quiet hours left out.
        if gap or not all(s.known for s in self.sources.values()):
            self._quiet_s = 0.0
            return self.now
        if not await self._quiet_hours(now):
            self._quiet_s += dt
        need = QUIET_S if len(self.sources) >= 2 else QUIET_ONE_S
        if self._quiet_s >= need and self.now.state != "away":
            began = self._last_activity if self._last_activity is not None else now - dt
            return await self._conclude("away", began, "quiet", now, hours=round(need / 3600))
        return self.now

    def _reset(self, now: float) -> None:
        self._quiet_s = 0.0
        self._activity = []
        self._seen_since = None

    async def _conclude(
        self, state: State, began: float | None, why: str, now: float, **args: Any
    ) -> Conclusion:
        if self.now.state == state and (state == "home" or self.now.why == why):
            # Still so. An absence is kept anew when the alarm takes over from quiet; home
            # stays as first concluded, whatever shows it again.
            return self.now
        self._seen_since = None
        found = Conclusion(state, now, began, why, args)
        body = json.dumps(args)

        def keep(t: Any) -> None:
            t.execute(
                "INSERT INTO occupancy (concluded, began, state, why, args) VALUES (?, ?, ?, ?, ?)",
                (now, began, state, why, body),
            )

        await self._db.run(keep)
        self.now = found
        log.info("occupancy: %s (%s)", state, why)
        return found

    @property
    def seen_since(self) -> float | None:
        """During an absence, the latest activity seen: not yet enough to conclude a return."""
        return self._seen_since

    async def recent(self, since: float) -> list[Conclusion]:
        def read(t: Any) -> list[tuple[Any, ...]]:
            return list(
                t.execute(
                    "SELECT concluded, began, state, why, args FROM occupancy"
                    " WHERE concluded >= ? ORDER BY id",
                    (since,),
                )
            )

        rows = await self._db.run(read)
        return [Conclusion(s, c, b, w, json.loads(a)) for c, b, s, w, a in rows]
