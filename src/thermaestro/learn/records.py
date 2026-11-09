"""What the learners keep in `learn_events`, as types: each kind of row a model, written
and read only here, so whoever reads a kind sees every shape it can have.

A day is either read or passed over because its readings had a gap (`Gap`). A reader that
takes a day's counts has to tell the two apart, or the type check fails.

Rows are kept 400 days, so a row kept before a field was added has to read too: such a
field has a default. One that doesn't read is an error, never skipped.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, TypeAdapter

from .. import clock
from ..store import Database, Transaction
from .electric import Edge

KEEP_S = 400 * 86_400.0
"""How long a row is kept."""


class Record(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Gap(Record):
    """A day passed over: its readings stopped for more than an hour, so what happened
    in it isn't known, which isn't the same as nothing."""

    day: date
    skipped: Literal["gap"]


class TankDay(Record):
    """A tank's day as read."""

    day: date
    hourly: list[float]
    """°C of the tank drawn in each hour."""
    predicted: list[float] | None
    """What the week predicted for the day before it learned from it; None while its
    weekday hadn't been seen."""
    baseline: list[float] | None
    draws: int
    drawn: float
    charges: int
    k: float
    """The standing loss used, per hour."""
    opens: float
    """What opened a draw that day."""
    season: float


class DayKind(Record):
    """A day's kind, as whether anyone was home was known when the tank was last learned:
    home, away, a departure, a return, or not known; and whether it was a free day."""

    day: date
    kind: Literal["home", "away", "departure", "return", "unknown"]
    free: bool


class DrawSeen(Record):
    start: float
    end: float
    size: float


class ChargeSeen(Record):
    end: float
    top0: float | None
    top1: float | None


class OfferDismissed(Record):
    id: str


class CompressorDay(Record):
    """A compressor's day as read; the electricity only where a meter is on the pump."""

    day: date
    starts: int
    run_s: dict[str, float]
    """How long the compressor ran, by purpose."""
    steps: int
    kwh: float | None = None
    addition_kwh: float | None = None
    standby_kw: float | None = None


class PowerStep(Record):
    edge: Edge
    purpose: str
    kw: float


class PowerRun(Record):
    purpose: str
    kw: float


class CopDay(Record):
    """Marks a day read for the COP."""

    day: date


class CopRun(Record):
    run_s: float
    supply_c: float
    source_c: float
    brine_ks: float
    kwh: float | None = None


class CopCharge(CopRun):
    rise_c: float = 0.0
    loss_kh: float = 0.0
    """Kept apart from the rise from 2026-10-19; in the rise before."""


class HouseHour(Record):
    """An hour of a climate system. `back` (the return) was kept from 2026-10-19: whether
    it was is in `model_fields_set`, since the water is worked out differently without."""

    t: float
    room: float | None
    outdoor: float | None
    water: float | None
    sun: float | None = None
    wind: float | None = None
    skip: str | None = None
    supply: float | None = None
    offset: float | None = None
    heating: float | None = None
    target: float | None = None
    back: float | None = None


class HouseDay(Record):
    day: date
    hours: list[HouseHour]


class PoolHour(Record):
    t: float
    pool: float | None
    outdoor: float | None
    share: float | None


class PoolDay(Record):
    day: date
    hours: list[PoolHour]


@dataclass(frozen=True)
class Kind[T: Record]:
    """A kind of row: its name in the table and what it reads as."""

    name: str
    shape: TypeAdapter[T]

    def read(self, body: str) -> T:
        return self.shape.validate_json(body)


def _kind[T: Record](name: str, shape: Any) -> "Kind[T]":
    return Kind(name, TypeAdapter(shape))


TANK_DAY: Kind[TankDay | Gap] = _kind("day", TankDay | Gap)
DAY_KIND: Kind[DayKind] = _kind("day_kind", DayKind)
DRAW: Kind[DrawSeen] = _kind("draw", DrawSeen)
CHARGE: Kind[ChargeSeen] = _kind("charge", ChargeSeen)
OFFER_DISMISSED: Kind[OfferDismissed] = _kind("offer_dismissed", OfferDismissed)
COMPRESSOR_DAY: Kind[CompressorDay | Gap] = _kind("compressor_day", CompressorDay | Gap)
POWER_STEP: Kind[PowerStep] = _kind("power_step", PowerStep)
POWER_RUN: Kind[PowerRun] = _kind("power_run", PowerRun)
COP_DAY: Kind[CopDay] = _kind("cop_day", CopDay)
COP_CHARGE: Kind[CopCharge] = _kind("cop_charge", CopCharge)
COP_HOUR: Kind[CopRun] = _kind("cop_hour", CopRun)
HOUSE_DAY: Kind[HouseDay] = _kind("house_day", HouseDay)
POOL_DAY: Kind[PoolDay] = _kind("pool_day", PoolDay)
KINDS: tuple[Kind[Any], ...] = (
    TANK_DAY,
    DAY_KIND,
    DRAW,
    CHARGE,
    OFFER_DISMISSED,
    COMPRESSOR_DAY,
    POWER_STEP,
    POWER_RUN,
    COP_DAY,
    COP_CHARGE,
    COP_HOUR,
    HOUSE_DAY,
    POOL_DAY,
)


@dataclass(frozen=True)
class Row:
    """A row to keep: made only by `row`, which checks the record fits its kind."""

    kind: str
    t: float
    body: str


def row[T: Record](kind: Kind[T], t: float, record: T) -> Row:
    return Row(kind.name, t, record.model_dump_json())


def keep(t: Transaction, scope: str, rows: Sequence[Row]) -> None:
    """Keep `rows` for `scope`, each in place of one of its kind at the same time; and
    let go of what is older than `KEEP_S`."""
    t.executemany(
        "INSERT OR REPLACE INTO learn_events (scope, kind, t, body) VALUES (?, ?, ?, ?)",
        [(scope, r.kind, r.t, r.body) for r in rows],
    )
    t.execute("DELETE FROM learn_events WHERE t < ?", (clock.time() - KEEP_S,))


async def read[T: Record](
    db: Database, scope: str, kind: Kind[T], since: float = 0.0
) -> list[tuple[float, T]]:
    """`scope`'s rows of `kind` from `since` on, oldest first."""

    def rows(t: Transaction) -> list[tuple[float, str]]:
        return list(
            t.execute(
                "SELECT t, body FROM learn_events WHERE scope = ? AND kind = ? AND t >= ?"
                " ORDER BY t",
                (scope, kind.name, since),
            )
        )

    return [(at, kind.read(body)) for at, body in await db.run(rows)]


async def last[T: Record](db: Database, scope: str, kind: Kind[T]) -> tuple[float, T] | None:
    """`scope`'s newest row of `kind`."""

    def newest(t: Transaction) -> tuple[float, str] | None:
        found = t.execute(
            "SELECT t, body FROM learn_events WHERE scope = ? AND kind = ? ORDER BY t DESC LIMIT 1",
            (scope, kind.name),
        ).fetchone()
        return None if found is None else (found[0], found[1])

    found = await db.run(newest)
    return None if found is None else (found[0], kind.read(found[1]))


async def every[T: Record](db: Database, kind: Kind[T]) -> list[tuple[str, float, T]]:
    """Every scope's rows of `kind`, as `(scope, t, record)`."""

    def rows(t: Transaction) -> list[tuple[str, float, str]]:
        return list(
            t.execute("SELECT scope, t, body FROM learn_events WHERE kind = ?", (kind.name,))
        )

    return [(scope, at, kind.read(body)) for scope, at, body in await db.run(rows)]
