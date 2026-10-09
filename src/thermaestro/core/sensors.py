"""Sensors from outside the pump, and what the core makes of them: rooms and the outdoors.

A sensor is an MQTT topic or a point some plugin offers (a Home Assistant entity, or the
pump's own outdoor sensor). Each one's values are kept under the instance `sensors`, as
`<sensor>/<quantity>`, with its calibration offset applied and its staleness checked.

From them the core derives, under the instance `site`:
- each room's values, `room.<room>/<quantity>`: the room's reference sensor for the
  quantity if it has one and it's good, else the mean of its good sensors (any open, for
  windows and zones);
- the outdoor values, `outdoor/<quantity>`, from the reference chosen per quantity; for
  temperature, the pump's own outdoor sensor where none is chosen;
- dew point and absolute humidity wherever temperature and relative humidity meet.

A derived value is never better than its inputs: with none good, it is `unknown`, and
says why.
"""

import json
import logging
import math
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from .. import clock, durations
from ..cap.model import Envelope, Quality
from ..cap.vocabulary import QUANTITIES, STATES
from ..store import Database, Display, Names, Outdoor, Room, Sensor
from .values import DAY_S, HELD_MAX_S, Key, Values

log = logging.getLogger(__name__)

SENSORS = "sensors"
SITE = "site"
DEFAULT_FRESHNESS_S = 3600.0
"""The least time a sensor may stay quiet before it is stale, and the limit until its
rhythm is known."""
MAX_FRESHNESS_S = HELD_MAX_S
"""The most: a reading stops counting after twelve hours, however slowly a sensor
reports."""
RHYTHM_DAYS = 7
RHYTHM_FACTOR = 2.0
"""Many sensors report only on a change, so a quiet one isn't necessarily a dead one. A
sensor is stale once it has been quiet for twice its longest silence over the last week,
within those bounds, unless its setting says otherwise."""

ROOM_UNITS = {"heat_demand": "%", "setpoint": "degC"}
ANY_OPEN = {"zone.open", "window.open", *STATES}
"""On/off quantities: a room's is on when any of its sensors is."""

Value = float | bool | str | None


@dataclass
class Reading:
    value: Value
    quality: Quality
    why: str | None
    t: float
    """When observed, seconds since the epoch."""
    received: float


def dew_point(t: float, rh: float) -> float:
    """Magnus formula (Sonntag's constants), within about 0.1 K from -45 to 60 °C."""
    a, b = 17.62, 243.12
    gamma = math.log(max(rh, 0.1) / 100.0) + a * t / (b + t)
    return b * gamma / (a - gamma)


def relative_humidity(t: float, dew: float) -> float:
    """The inverse of `dew_point`: relative humidity in % from temperature and dew point."""
    a, b = 17.62, 243.12
    return min(100.0, 100.0 * math.exp(a * dew / (b + dew) - a * t / (b + t)))


def absolute_humidity(t: float, rh: float) -> float:
    """Grams of water per cubic meter of air."""
    saturation = 6.112 * math.exp(17.67 * t / (t + 243.5))  # hPa
    return saturation * rh * 2.1674 / (273.15 + t)


def parse(payload: bytes, json_key: str | None) -> Value:
    """An MQTT payload's value: a bare number or on/off word, or a JSON field."""
    text = payload.decode("utf-8", errors="replace").strip()
    data: Any = text
    if json_key:
        try:
            data = json.loads(text)
            for part in json_key.split("."):
                data = data[part]
        except (ValueError, KeyError, TypeError, IndexError):
            return None
    return _coerce(data)


def _coerce(data: Any) -> Value:
    if isinstance(data, bool):
        return data
    if isinstance(data, int | float):
        return float(data)
    if isinstance(data, str):
        word = data.strip().lower()
        if word in ("on", "open", "true", "yes", "detected"):
            return True
        if word in ("off", "closed", "false", "no", "clear"):
            return False
        try:
            return float(word)
        except ValueError:
            return None
    return None


class SensorHub:
    def __init__(self, db: Database, values: Values, clock: Callable[[], float] = time.time):
        self._db = db
        self._values = values
        self._clock = clock
        self.sensors: dict[str, Sensor] = {}
        self.rooms: dict[str, Room] = {}
        self.outdoor = Outdoor()
        self.names: dict[str, str] = {}
        self.display = Display()
        self._readings: dict[str, Reading] = {}
        self._by_point: dict[str, list[str]] = {}
        self._by_topic: dict[str, list[str]] = {}
        self._silences: dict[str, dict[int, float]] = {}
        """Each sensor's longest silence, by day (days since the epoch, UTC)."""
        self._unsaved: set[tuple[str, int]] = set()
        self._forgotten: int | None = None
        values.listeners.append(self._heard)

    # --- settings --------------------------------------------------------------------------

    async def load(self) -> None:
        """Read the sensor, room, outdoor and name settings again, after a change."""

        def read(t: Any) -> tuple[Any, ...]:
            silences = list(t.execute("SELECT sensor, day, longest FROM sensor_silences"))
            settings = t.all(Sensor), t.all(Room), t.get(Outdoor), t.get(Names), t.get(Display)
            return *settings, silences

        sensors, rooms, outdoor, names, display, silences = await self._db.run(read)
        for id, day, longest in silences:
            days = self._silences.setdefault(id, {})
            days[day] = max(days.get(day, 0.0), longest)
        self._silences = {id: s for id, s in self._silences.items() if id in sensors}
        self.display = display or Display()
        self.sensors, self.rooms = sensors, rooms
        self.outdoor = outdoor or Outdoor()
        self.names = dict(names.names) if names else {}
        self._by_point, self._by_topic = {}, {}
        for id, sensor in sensors.items():
            if sensor.source == "point" and sensor.point:
                self._by_point.setdefault(sensor.point, []).append(id)
            elif sensor.topic:
                self._by_topic.setdefault(sensor.topic, []).append(id)
        self._readings = {id: r for id, r in self._readings.items() if id in sensors}
        for key, envelope in list(self._values.latest.items()):
            if key.instance not in (SENSORS, SITE):
                self._heard(key.instance, envelope)
        self._derive()

    def topics(self) -> list[str]:
        return sorted(self._by_topic)

    # --- what comes in ---------------------------------------------------------------------

    def receive_mqtt(self, topic: str, payload: bytes) -> None:
        now = self._clock()
        for id in self._by_topic.get(topic, ()):
            sensor = self.sensors[id]
            value = parse(payload, sensor.json_key)
            if value is None:
                self._accept(id, Reading(None, "unknown", "the payload isn't a value", now, now))
            else:
                self._accept(id, Reading(value, "good", None, now, now))

    def _heard(self, instance: str, envelope: Envelope) -> None:
        if instance in (SENSORS, SITE):
            return
        ids = self._by_point.get(f"{instance}:{envelope.point}", [])
        now = self._clock()
        t = (envelope.t_observed or envelope.t_received).timestamp()
        for id in ids:
            self._accept(id, Reading(envelope.value, envelope.quality, envelope.why, t, now))
        if not ids and envelope.point.endswith("/outdoor.temp"):
            self._derive()  # the default outdoor reference

    def _accept(self, id: str, reading: Reading) -> None:
        sensor = self.sensors[id]
        value = reading.value
        if isinstance(value, int | float) and not isinstance(value, bool):
            reading.value = float(value) + sensor.calibration_offset
        previous = self._readings.get(id)
        if previous is not None and reading.t > previous.t:
            self._silence(id, reading.t - previous.t, reading.t)
        self._readings[id] = reading
        self._publish(id)
        self._derive()

    def _silence(self, id: str, seconds: float, ended: float) -> None:
        """A silence between two reports, kept where it is the day's longest. One across
        a restart isn't seen: the sensor wasn't the one quiet."""
        day = int(ended // DAY_S)
        days = self._silences.setdefault(id, {})
        if seconds > days.get(day, 0.0):
            days[day] = seconds
            self._unsaved.add((id, day))

    def freshness(self, id: str) -> tuple[float, bool]:
        """How long the sensor may stay quiet before it is stale, and whether that was
        learned from its rhythm rather than set or the default."""
        set_s = self.sensors[id].freshness_s
        if set_s is not None:
            return min(set_s, MAX_FRESHNESS_S), False
        today = int(self._clock() // DAY_S)
        week = [s for d, s in self._silences.get(id, {}).items() if d > today - RHYTHM_DAYS]
        if not week:
            return DEFAULT_FRESHNESS_S, False
        learned = RHYTHM_FACTOR * max(week)
        return min(max(learned, DEFAULT_FRESHNESS_S), MAX_FRESHNESS_S), True

    def tick(self) -> None:
        """Mark sensors that have gone quiet as stale; call regularly."""
        changed = False
        for id, reading in self._readings.items():
            if reading.quality == "good" and self._stale(id, reading):
                changed = True
                self._publish(id)
        if changed:
            self._derive()

    async def save(self) -> None:
        """Keep the silences learned since the last save, and forget those older than a
        week."""
        unsaved, self._unsaved = self._unsaved, set()
        rows = [
            (id, day, self._silences[id][day])
            for id, day in unsaved
            if day in self._silences.get(id, {})
        ]
        oldest = int(self._clock() // DAY_S) - RHYTHM_DAYS

        def write(t: Any) -> None:
            t.executemany(
                "INSERT OR REPLACE INTO sensor_silences (sensor, day, longest) VALUES (?, ?, ?)",
                rows,
            )
            t.execute("DELETE FROM sensor_silences WHERE day <= ?", (oldest,))

        if rows or oldest != self._forgotten:  # old days are forgotten once a day
            try:
                await self._db.run(write)
            except BaseException:
                self._unsaved |= unsaved  # for the next save
                raise
            self._forgotten = oldest
        for days in self._silences.values():
            for day in [d for d in days if d <= oldest]:
                del days[day]

    def _stale(self, id: str, reading: Reading) -> bool:
        return self._clock() - reading.received > self.freshness(id)[0]

    def _current(self, id: str) -> Reading | None:
        reading = self._readings.get(id)
        if reading is None:
            return None
        if reading.quality == "good" and self._stale(id, reading):
            age = durations.text(int(self._clock() - reading.received))
            return Reading(
                reading.value, "stale", f"no report for {age}", reading.t, reading.received
            )
        return reading

    # --- what goes out ---------------------------------------------------------------------

    def _publish(self, id: str) -> None:
        sensor = self.sensors[id]
        reading = self._current(id)
        if reading is None:
            return
        self._values.add(
            SENSORS,
            _envelope(
                f"{id}/{sensor.quantity}",
                reading.value,
                _unit(sensor.quantity),
                reading.quality,
                reading.why,
                reading.t,
                "measured",
            ),
        )

    def _derive(self) -> None:
        for room_id in self.rooms:
            members = [
                i for i, s in self.sensors.items() if s.placement == "room" and s.room == room_id
            ]
            for quantity in sorted({self.sensors[i].quantity for i in members}):
                group = [i for i in members if self.sensors[i].quantity == quantity]
                self._combine(f"room.{room_id}", quantity, group)
            self._humidity(f"room.{room_id}")
        outdoor = self._outdoor_references()
        for quantity, source in outdoor.items():
            self._put(f"outdoor/{quantity}", quantity, *source)
        self._humidity("outdoor")

    def _combine(self, node: str, quantity: str, ids: list[str]) -> None:
        readings = [(i, r) for i in ids if (r := self._current(i)) is not None]
        good = [(i, r) for i, r in readings if r.quality == "good"]
        reference = [r for i, r in good if self.sensors[i].reference]
        if reference:
            chosen = reference[0]
            self._put(f"{node}/{quantity}", quantity, chosen.value, "good", None, chosen.t)
            return
        if not good:
            missing = "no value yet" if not readings else "no sensor with a good value"
            self._put(f"{node}/{quantity}", quantity, None, "unknown", missing, self._clock())
            return
        values = [r.value for _, r in good]
        t = max(r.t for _, r in good)
        if quantity in ANY_OPEN or all(isinstance(v, bool) for v in values):
            self._put(f"{node}/{quantity}", quantity, any(bool(v) for v in values), "good", None, t)
        else:
            numbers = [float(v) for v in values if isinstance(v, int | float)]
            mean = sum(numbers) / len(numbers) if numbers else None
            how = None if len(numbers) == 1 else f"mean of {len(numbers)} sensors"
            self._put(f"{node}/{quantity}", quantity, mean, "good", how, t)

    def _outdoor_references(self) -> dict[str, tuple[Value, Quality, str | None, float]]:
        out: dict[str, tuple[Value, Quality, str | None, float]] = {}
        for quantity, id in self.outdoor.references.items():
            reading = self._current(id) if id in self.sensors else None
            if reading is None:
                out[quantity] = (
                    None,
                    "unknown",
                    "the reference sensor gives no value",
                    self._clock(),
                )
            else:
                out[quantity] = (reading.value, reading.quality, reading.why, reading.t)
        if "temperature" not in out:
            pump = self._pump_outdoor()
            if pump is not None:
                t = (pump.t_observed or pump.t_received).timestamp()
                out["temperature"] = (pump.value, pump.quality, pump.why, t)
        return out

    def _pump_outdoor(self) -> Envelope | None:
        for key, envelope in sorted(self._values.latest.items(), key=lambda kv: kv[0].instance):
            if key.instance not in (SENSORS, SITE) and key.point.endswith("/outdoor.temp"):
                return envelope
        return None

    def _humidity(self, node: str) -> None:
        temp = self._values.latest.get(_key(node, "temperature"))
        rh = self._values.latest.get(_key(node, "humidity"))
        if temp is None or rh is None:
            return
        good = temp.quality == "good" and rh.quality == "good"
        t_value, rh_value = temp.value, rh.value
        if not good or not isinstance(t_value, float) or not isinstance(rh_value, float):
            for quantity in ("dew_point", "absolute_humidity"):
                self._put(
                    f"{node}/{quantity}",
                    quantity,
                    None,
                    "unknown",
                    "needs a good temperature and humidity",
                    self._clock(),
                    "calculated",
                )
            return
        t = max(
            (temp.t_observed or temp.t_received).timestamp(),
            (rh.t_observed or rh.t_received).timestamp(),
        )
        self._put(
            f"{node}/dew_point",
            "dew_point",
            round(dew_point(t_value, rh_value), 2),
            "good",
            None,
            t,
            "calculated",
        )
        self._put(
            f"{node}/absolute_humidity",
            "absolute_humidity",
            round(absolute_humidity(t_value, rh_value), 2),
            "good",
            None,
            t,
            "calculated",
        )

    def _put(
        self,
        path: str,
        quantity: str,
        value: Value,
        quality: Quality,
        why: str | None,
        t: float,
        source: str = "calculated",
    ) -> None:
        self._values.add(SITE, _envelope(path, value, _unit(quantity), quality, why, t, source))

    # --- for the pages ---------------------------------------------------------------------

    def site_points(self) -> list[str]:
        """The derived points there are now, rooms first."""
        return sorted(
            (k.point for k in self._values.latest if k.instance == SITE),
            key=lambda p: (not p.startswith("room."), p),
        )

    def readings(self) -> Iterable[tuple[str, Reading | None]]:
        for id in sorted(self.sensors, key=lambda i: self.sensors[i].name.lower()):
            yield id, self._current(id)


def _key(node: str, quantity: str) -> Key:
    return Key(SITE, f"{node}/{quantity}")


def _unit(quantity: str) -> str | None:
    if quantity in ("dew_point",):
        return "degC"
    return QUANTITIES.get(quantity) or ROOM_UNITS.get(quantity)


def _envelope(
    point: str,
    value: Value,
    unit: str | None,
    quality: Quality,
    why: str | None,
    t: float,
    source: str,
) -> Envelope:
    observed = datetime.fromtimestamp(t, UTC)
    return Envelope.model_validate(
        {
            "point": point,
            "value": value,
            "unit": unit if value is not None and not isinstance(value, bool) else None,
            "t_observed": observed,
            "t_received": clock.now(),
            "quality": quality,
            "source": source,
            "why": why,
        }
    )
