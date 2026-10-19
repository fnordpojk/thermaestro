"""A bath is judged at its time: hot before then, it stays open, so the plan keeps the
water hot for it."""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

from thermaestro.cap.model import Envelope
from thermaestro.core import AuditLog, Values
from thermaestro.intents import kinds
from thermaestro.planner import Planner
from thermaestro.store import Database

NOW = datetime(2026, 10, 20, 15, 0, tzinfo=UTC)
TANK = "pump:hp1/dhw"


class Asked:
    def __init__(self, intent: Any) -> None:
        self.intent = intent
        self.reported: list[tuple[str, str]] = []

    async def all(self) -> list[Any]:
        return [self.intent]

    async def report(self, id: str, state: str, why: str) -> None:
        self.reported.append((id, state))


def top(value: float, at: datetime) -> Envelope:
    return Envelope(
        point="hp1/dhw/temp.top",
        value=value,
        unit="degC",
        t_observed=at,
        t_received=at,
        quality="good",
        source="measured",
    )


async def test_a_bath_hot_early_is_met_at_its_time(tmp_path: Path) -> None:
    by = NOW + timedelta(hours=3)
    bath = kinds.bath(TANK, 50.0, by, principal="user:anna", created=NOW)
    asked = Asked(bath.model_copy(update={"state": "active"}))
    async with await Database.open(tmp_path / "t.db") as db:
        values = Values(db)
        values.add("pump", top(52.0, NOW))
        nothing = cast(Any, None)
        planner = Planner(
            db, values, nothing, nothing, cast(Any, asked), nothing, AuditLog(tmp_path / "audit")
        )
        await planner._follow_up(NOW)
        assert asked.reported == []  # hot already, but open until its time
        await planner._follow_up(by)
        assert asked.reported == [(bath.id, "met")]
