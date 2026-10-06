"""The latest value of every point, and its history.

A pump pushes some values twice a second, and a Pi's SD card wears with every write. So
a value goes into the history only when it changes, when its quality changes, or when
`heartbeat_s` has passed since the last one kept; and the history is written in batches.
Old samples are folded into 15-minute aggregates, which are kept far longer.
"""

import asyncio
import logging
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass

from ..cap.model import Envelope, Point
from ..store import Database, Transaction
from .plausible import Plausibility

log = logging.getLogger(__name__)

SLOT_S = 900.0
DAY_S = 86_400.0


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
        now = time.time() if now is None else now
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
                    if time.monotonic() - last_prune >= prune_s:
                        await self.prune(raw_days, aggregate_days)
                        last_prune = time.monotonic()
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


def _prune(t: Transaction, raw_cutoff: float, aggregate_cutoff: float) -> None:
    # Only good numeric values are aggregated: a sensor that wasn't connected has no
    # temperature to average.
    t.execute(
        """
        INSERT OR IGNORE INTO history_15m (instance, point, slot, min, mean, max, last, n)
        SELECT instance, point, slot, MIN(value), AVG(value), MAX(value),
               (SELECT h2.value FROM history h2
                 WHERE h2.instance = g.instance AND h2.point = g.point
                   AND h2.quality = 'good' AND h2.value IS NOT NULL
                   AND h2.t >= g.slot AND h2.t < g.slot + ?
                 ORDER BY h2.t DESC LIMIT 1),
               COUNT(*)
          FROM (SELECT instance, point, value, CAST(t / ? AS INTEGER) * ? AS slot
                  FROM history
                 WHERE t < ? AND quality = 'good' AND value IS NOT NULL) AS g
         GROUP BY instance, point, slot
        """,
        (SLOT_S, SLOT_S, SLOT_S, raw_cutoff),
    )
    t.execute("DELETE FROM history WHERE t < ?", (raw_cutoff,))
    t.execute("DELETE FROM history_15m WHERE slot < ?", (aggregate_cutoff,))
