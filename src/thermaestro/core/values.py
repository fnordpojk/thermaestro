"""The latest value of every point, and its history.

A pump pushes some values twice a second, and a Pi's SD card wears with every write. So
a value goes into the history only when it changes, when its quality changes, or when
`heartbeat_s` has passed since the last one kept; and the history is written in batches.
Old samples are folded into 15-minute aggregates, which are kept far longer.

Means are over time, not over samples: a value counts for as long as it held, until the
next sample, for at most `HELD_MAX_S`. A sensor that reports every few minutes while the
temperature moves and once in hours while it doesn't would otherwise pull the mean toward
the moving stretches.
"""

import asyncio
import logging
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, tzinfo
from itertools import groupby

from .. import clock
from ..cap.model import Envelope, Point
from ..store import Database, Transaction
from .plausible import Plausibility

log = logging.getLogger(__name__)

SLOT_S = 900.0
DAY_S = 86_400.0
HELD_MAX_S = 12 * 3600.0
"""How long a value counts for with nothing after it: a reading stops counting after
twelve hours, however slowly its sensor reports."""

Row = tuple[float, float | None, str]
"""A sample as the history keeps it: when, its number, its quality."""


@dataclass(frozen=True, slots=True)
class Key:
    """A point of one plugin instance; two instances may use the same paths."""

    instance: str
    point: str


@dataclass(frozen=True, slots=True)
class Sample:
    t: float
    """Seconds since the epoch: when observed, or when received if that's unknown."""
    value: float | None
    text: str | None
    quality: str


def sample(envelope: Envelope) -> Sample:
    t = (envelope.t_observed or envelope.t_received).timestamp()
    v = envelope.value
    if isinstance(v, bool):
        return Sample(t, float(v), None, envelope.quality)
    if isinstance(v, int | float):
        return Sample(t, float(v), None, envelope.quality)
    return Sample(t, None, v, envelope.quality)


class Values:
    def __init__(self, db: Database, *, heartbeat_s: float = 300.0) -> None:
        self._db = db
        self.heartbeat_s = heartbeat_s
        self.latest: dict[Key, Envelope] = {}
        self._kept: dict[Key, Sample] = {}
        self._pending: list[tuple[Key, Sample]] = []
        self._plausibility = Plausibility()
        self.listeners: list[Callable[[str, Envelope], None]] = []
        """Told of every value as it is kept, after the plausibility check."""

    def add(self, instance: str, envelope: Envelope, point: Point | None = None) -> None:
        """Keep a value, after the core's own plausibility check (`point`, where known,
        says how its counter wraps)."""
        envelope = self._plausibility.check(instance, envelope, point)
        key = Key(instance, envelope.point)
        self.latest[key] = envelope
        for listener in self.listeners:
            listener(instance, envelope)
        new = sample(envelope)
        kept = self._kept.get(key)
        if kept is not None:
            if new.t <= kept.t:
                return  # not newer than what's kept: a re-sent value
            same = (new.value, new.text, new.quality) == (kept.value, kept.text, kept.quality)
            if same and new.t - kept.t < self.heartbeat_s:
                return
        self._kept[key] = new
        self._pending.append((key, new))

    @property
    def pending(self) -> int:
        return len(self._pending)

    async def flush(self) -> int:
        """Write what's waiting, in one transaction."""
        rows, self._pending = self._pending, []
        if not rows:
            return 0
        try:
            await self._db.run(lambda t: _insert(t, rows))
        except BaseException:
            self._pending[:0] = rows  # kept for the next try
            raise
        return len(rows)

    async def prune(self, raw_days: int, aggregate_days: int, now: float | None = None) -> None:
        """Fold samples older than `raw_days` into 15-minute aggregates and drop them;
        drop aggregates older than `aggregate_days`."""
        now = clock.time() if now is None else now
        raw_cutoff = (now - raw_days * DAY_S) // SLOT_S * SLOT_S
        aggregate_cutoff = now - aggregate_days * DAY_S
        await self._db.run(lambda t: _prune(t, raw_cutoff, aggregate_cutoff))

    async def history(self, key: Key, start: float, end: float) -> list[Sample]:
        def read(t: Transaction) -> list[Sample]:
            rows = t.execute(
                "SELECT t, value, text, quality FROM history"
                " WHERE instance = ? AND point = ? AND t >= ? AND t < ? ORDER BY t",
                (key.instance, key.point, start, end),
            )
            return [Sample(*row) for row in rows]

        return await self._db.run(read)

    async def first(self, key: Key) -> float | None:
        """When the point's history starts, or None where it has none."""

        def read(t: Transaction) -> float | None:
            found = [
                row[0]
                for table, column in (("history", "t"), ("history_15m", "slot"))
                for row in t.execute(
                    f"SELECT MIN({column}) FROM {table} WHERE instance = ? AND point = ?",  # noqa: S608 - names, not input
                    (key.instance, key.point),
                )
                if row[0] is not None
            ]
            return min(found, default=None)

        return await self._db.run(read)

    async def daily(
        self, key: Key, start: float, end: float, zone: tzinfo
    ) -> list[tuple[date, float, float, float]]:
        """Each local day's lowest, mean and highest good value, from the 15-minute
        aggregates where the samples were folded into them and the samples since; the mean
        over the time each value held."""
        end = min(end, clock.time())

        def read(t: Transaction) -> list[tuple[date, float, float, float]]:
            days: dict[date, list[float]] = {}  # low, value-seconds, seconds, high

            def add(d: date, low: float, total: float, seconds: float, high: float) -> None:
                found = days.get(d)
                if found is None:
                    days[d] = [low, total, seconds, high]
                else:
                    found[0] = min(found[0], low)
                    found[1] += total
                    found[2] += seconds
                    found[3] = max(found[3], high)

            folded_until = start
            for slot, low, mean, high in t.execute(
                "SELECT slot, min, mean, max FROM history_15m"
                " WHERE instance = ? AND point = ? AND slot >= ? AND slot < ? ORDER BY slot",
                (key.instance, key.point, start, end),
            ):
                add(datetime.fromtimestamp(slot, zone).date(), low, mean * SLOT_S, SLOT_S, high)
                folded_until = slot + SLOT_S
            rows = t.execute(
                "SELECT t, value, quality FROM history"
                " WHERE instance = ? AND point = ? AND t >= ? AND t < ? ORDER BY t",
                (key.instance, key.point, folded_until - HELD_MAX_S, end),
            )
            for t0, t1, value in _held(rows, end):
                t0 = max(t0, folded_until)
                while t0 < t1:
                    d = datetime.fromtimestamp(t0, zone).date()
                    midnight = datetime.combine(d + timedelta(days=1), time(), zone)
                    stop = min(t1, midnight.timestamp())
                    add(d, value, value * (stop - t0), stop - t0, value)
                    t0 = stop
            return [(d, v[0], v[1] / v[2], v[3]) for d, v in sorted(days.items())]

        return await self._db.run(read)

    async def run(
        self, *, flush_s: float, prune_s: float, raw_days: int, aggregate_days: int
    ) -> None:
        """Flush every `flush_s`, prune every `prune_s`, until cancelled; flush once more
        on the way out."""
        last_prune = 0.0
        try:
            while True:
                await asyncio.sleep(flush_s)
                try:
                    await self.flush()
                    if clock.monotonic() - last_prune >= prune_s:
                        await self.prune(raw_days, aggregate_days)
                        last_prune = clock.monotonic()
                except Exception:
                    log.exception("writing the value history failed; trying again later")
        finally:
            await self.flush()


def _insert(t: Transaction, rows: Iterable[tuple[Key, Sample]]) -> None:
    t.executemany(
        "INSERT OR REPLACE INTO history (instance, point, t, value, text, quality)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        [(k.instance, k.point, s.t, s.value, s.text, s.quality) for k, s in rows],
    )


def _held(rows: Iterable[Row], end: float) -> Iterator[tuple[float, float, float]]:
    """Each good value, from when it was seen until the next sample, `end`, or
    `HELD_MAX_S`, whichever comes first. Only good numeric values count: a sensor that
    wasn't connected has no temperature to average."""
    good: tuple[float, float] | None = None
    for at, value, quality in rows:
        if good is not None:
            stop = min(at, end, good[0] + HELD_MAX_S)
            if stop > good[0]:
                yield good[0], stop, good[1]
        good = (at, value) if quality == "good" and value is not None else None
    if good is not None:
        stop = min(end, good[0] + HELD_MAX_S)
        if stop > good[0]:
            yield good[0], stop, good[1]


@dataclass
class _Slot:
    low: float
    high: float
    total: float = 0.0
    """Value-seconds."""
    seconds: float = 0.0
    last: float = 0.0
    n: int = 0


def _fold(rows: list[Row], cutoff: float) -> dict[float, _Slot]:
    """One point's samples before `cutoff` as 15-minute slots, each value spread over the
    slots it held in; a slot where a value only held on has no samples of its own."""
    slots: dict[float, _Slot] = {}
    for t0, t1, value in _held(rows, cutoff):
        slot = t0 // SLOT_S * SLOT_S
        first = True
        while slot < t1:
            seconds = min(t1, slot + SLOT_S) - max(t0, slot)
            found = slots.setdefault(slot, _Slot(low=value, high=value))
            found.low, found.high = min(found.low, value), max(found.high, value)
            found.total += value * seconds
            found.seconds += seconds
            found.last = value
            found.n += first
            first = False
            slot += SLOT_S
    return slots


def _prune(t: Transaction, raw_cutoff: float, aggregate_cutoff: float) -> None:
    rows = t.execute(
        "SELECT instance, point, t, value, quality FROM history WHERE t < ?"
        " ORDER BY instance, point, t",
        (raw_cutoff,),
    )
    folded: list[tuple[str, str, float, float, float, float, float, int]] = []
    dropped: list[tuple[str, str, float]] = []
    for (instance, point), group in groupby(rows, key=lambda r: (r[0], r[1])):
        samples: list[Row] = [(at, value, quality) for _, _, at, value, quality in group]
        for slot, s in _fold(samples, raw_cutoff).items():
            mean = s.total / s.seconds
            folded.append((instance, point, slot, s.low, mean, s.high, s.last, s.n))
        # The newest sample may hold on past the cutoff: it stays, so the next fold
        # starts from it. Its own slots are folded already and aren't folded again.
        newest = samples[-1][0]
        dropped.append(
            (instance, point, newest if newest >= raw_cutoff - HELD_MAX_S else raw_cutoff)
        )
    t.executemany(
        "INSERT OR IGNORE INTO history_15m (instance, point, slot, min, mean, max, last, n)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        folded,
    )
    t.executemany("DELETE FROM history WHERE instance = ? AND point = ? AND t < ?", dropped)
    t.execute("DELETE FROM history_15m WHERE slot < ?", (aggregate_cutoff,))
