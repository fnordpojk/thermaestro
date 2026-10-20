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
says why. It takes its inputs' time, which can go back (a sensor dropping out of a mean,
a good reading after none): a value observed before the one shown would be dropped, so a
change that would be is placed when it is made.
"""

import json
import logging
import math
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, cast

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
COUNTER_EVERY_S = 10.0
"""An energy counter that reports at every pulse is kept at most this often: what it
counted in between is in the next reading, and a Pi's SD card isn't written several
times a second. A counter that falls (it started over) is always kept."""
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
    live: bool = True
    """Heard while running: not the last value remembered from before a restart, nor
    one the broker kept."""


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
        self._up: dict[str | None, float] = {}
        """When each source last connected: the broker (None), or a plugin instance."""
        values.listeners.append(self._heard)

    # --- settings --------------------------------------------------------------------------

    async def load(self) -> None:
        """Read the sensor, room, outdoor and name settings again, after a change. A
        sensor without a reading yet starts from its last one in the history, as old as
        it is: rooms are known at a restart, and a value the broker kept can be told
        apart from a new one."""

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
        unread = {
            id: Key(SENSORS, f"{id}/{s.quantity}")
            for id, s in sensors.items()
            if id not in self._readings
        }
        last = await self._values.last(unread.values()) if unread else {}
        for id, key in unread.items():
            found = last.get(key)
            if found is not None:
                value = _remembered(sensors[id].quantity, found.value, found.text)
                quality = cast(Quality, found.quality)
                self._readings[id] = Reading(value, quality, None, found.t, found.t, False)
                self._publish(id)
        for key, envelope in list(self._values.latest.items()):
            if key.instance not in (SENSORS, SITE):
                self._heard(key.instance, envelope)
        self._derive()

    def topics(self) -> list[str]:
        return sorted(self._by_topic)

    # --- what comes in ---------------------------------------------------------------------

    def connected(self, source: str | None = None) -> None:
        """The broker (None), or a plugin instance, has just connected: a silence that
        began before says nothing of its sensors."""
        self._up[source] = self._clock()

    def receive_mqtt(self, topic: str, payload: bytes, retained: bool = False) -> None:
        """A message on sensors' topic. A retained one is what the broker kept, of unknown
        age: the value already held is nothing new, and another is taken as stale until
        the sensor next reports."""
        now = self._clock()
        for id in self._by_topic.get(topic, ()):
            sensor = self.sensors[id]
            value = parse(payload, sensor.json_key)
            if value is None:
                why = "the payload isn't a value"
                self._accept(id, Reading(None, "unknown", why, now, now, not retained))
            elif not retained:
                self._accept(id, Reading(value, "good", None, now, now))
            else:
                held = self._readings.get(id)
                if held is None or held.value != _calibrated(sensor, value):
                    why = "kept by the broker, of unknown age"
                    self._accept(id, Reading(value, "stale", why, now, now, False))

    def _heard(self, instance: str, envelope: Envelope) -> None:
        if instance in (SENSORS, SITE):
            return
        ids = self._by_point.get(f"{instance}:{envelope.point}", [])
        now = self._clock()
        t = (envelope.t_observed or envelope.t_received).timestamp()
        for id in ids:
            value = envelope.value
            if (
                self.sensors[id].quantity == "energy"
                and envelope.unit == "Wh"
                and isinstance(value, int | float)
            ):
                value = float(value) / 1000  # kept in kWh, as the quantity is
            self._accept(id, Reading(value, envelope.quality, envelope.why, t, now))
        if not ids and envelope.point.endswith("/outdoor.temp"):
            self._derive()  # the default outdoor reference

    def _accept(self, id: str, reading: Reading) -> None:
        sensor = self.sensors[id]
        reading.value = _calibrated(sensor, reading.value)
        previous = self._readings.get(id)
        if _too_soon(sensor, previous, reading):
            return
        if previous is not None and reading.t > previous.t and self._own(sensor, previous, reading):
            self._silence(id, reading.t - previous.t, reading.t)
        self._readings[id] = reading
        self._publish(id)
        self._derive()

    def _own(self, sensor: Sensor, previous: Reading, reading: Reading) -> bool:
        """Whether the silence between two reports was the sensor's own: both heard while
        running, and its source (the broker, or the plugin) connected all along. A
        restart, or the broker or Home Assistant away, silences every sensor at once."""
        if not (previous.live and reading.live):
            return False
        up = self._up.get(_source(sensor))
        return up is None or previous.received >= up

    def _silence(self, id: str, seconds: float, ended: float) -> None:
        """A silence between two reports, kept where it is the day's longest."""
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
        path = f"{id}/{sensor.quantity}"
        t = self._in_order(
            Key(SENSORS, path), reading.value, reading.quality, reading.why, reading.t
        )
        self._values.add(
            SENSORS,
            _envelope(
                path,
                reading.value,
                _unit(sensor.quantity),
                reading.quality,
                reading.why,
                t,
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
        t = self._in_order(Key(SITE, path), value, quality, why, t)
        self._values.add(SITE, _envelope(path, value, _unit(quantity), quality, why, t, source))

    def _in_order(
        self, key: Key, value: Value, quality: Quality, why: str | None, t: float
    ) -> float:
        """`t`, or where that is before the value shown and this one differs from it, now:
        at the same time, it is a later word on the same reading, as a value's own is."""
        shown = self._values.latest.get(key)
        if shown is None or shown.t_observed is None:
            return t
        before = shown.t_observed.timestamp()
        if t >= before or (shown.value, shown.quality, shown.why) == (value, quality, why):
            return t
        return max(self._clock(), before + 0.001)

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


def _calibrated(sensor: Sensor, value: Value) -> Value:
    """A number as the sensor's settings make it: pulses to kWh, and its offset added."""
    if isinstance(value, int | float) and not isinstance(value, bool):
        if sensor.per_kwh is not None:
            value = float(value) / sensor.per_kwh
        return float(value) + sensor.calibration_offset
    return value


def _remembered(quantity: str, value: float | None, text: str | None) -> Value:
    """A value as the history keeps it, back as the sensor gave it: on and off are kept
    as 1 and 0."""
    if text is not None:
        return text
    if value in (0.0, 1.0) and _unit(quantity) is None:
        return bool(value)
    return value


def _source(sensor: Sensor) -> str | None:
    """What a sensor's values come through: the broker (None), or a plugin instance."""
    if sensor.source == "point" and sensor.point:
        return sensor.point.split(":", 1)[0]
    return None


def _too_soon(sensor: Sensor, previous: Reading | None, reading: Reading) -> bool:
    """An energy counter's reading too soon after the last kept one, and not a fall."""
    if sensor.quantity != "energy" or previous is None or previous.quality != reading.quality:
        return False
    before, now = previous.value, reading.value
    if isinstance(before, float) and isinstance(now, float) and now < before:
        return False
    return reading.t - previous.t < COUNTER_EVERY_S


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
