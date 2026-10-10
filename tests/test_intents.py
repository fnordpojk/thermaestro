"""Intents: their shape, what is in force when, how they combine, the checks on the way
in, keeping them with rights and an audit trail, and seeding from the house."""

import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator
from pydantic import ValidationError

from thermaestro.core import AuditLog
from thermaestro.intents import (
    Calendar,
    Capabilities,
    Forbidden,
    Found,
    Intent,
    Intents,
    Level,
    Resolver,
    check,
    kinds,
    schema,
)
from thermaestro.intents.model import Validity
from thermaestro.intents.requests import build, request_of
from thermaestro.intents.resolve import RANKS
from thermaestro.store import Database, Home, Location

ZONE = "Europe/Stockholm"
CAL = Calendar(ZONE, "SE")
CS = "pump:hp1/cs1"
TANK = "pump:hp1/dhw"
ROOM = "room:bedroom"
T0 = datetime(2026, 12, 21, 6, 0, tzinfo=UTC)  # a Monday, 07:00 in Stockholm
WEEKDAYS = (0, 1, 2, 3, 4)

LEVELS = {
    level.id: level
    for level in (
        Level(id="day", name="Day", scope=CS, low=20.5, high=22.0),
        Level(id="night", name="Night", scope=CS, low=18.0, high=20.0),
        Level(id="away", name="Away", scope=CS, low=15.0, high=17.0),
        Level(id="cool", name="Cool", scope=ROOM, low=17.0, high=19.0),
        Level(id="tank", name="Normal", scope=TANK, top=50.0),
        Level(id="tank-more", name="More", scope=TANK, top=55.0),
    )
}


def local(day: int, hour: int, minute: int = 0) -> datetime:
    """December 2026, Stockholm time."""
    return datetime(2026, 12, day, hour, minute, tzinfo=CAL.zone)


def band(created: datetime = T0, **kw: object) -> Intent:
    """Day 06:00 to 22:00, night otherwise, every day."""
    return kinds.comfort_band(
        CS,
        [("day", ((), time(6), time(22))), ("night", ((), time(22), time(6)))],
        principal="user:anna",
        created=created,
        **kw,  # type: ignore[arg-type]
    )


def resolver(*intents: Intent, rooms: dict[str, str] | None = None) -> Resolver:
    active = [i.model_copy(update={"state": "active"}) for i in intents]
    return Resolver(active, LEVELS, CAL, rooms or {})


def low_high(r: Resolver, at: datetime, scope: str = CS) -> tuple[float | None, float | None]:
    found = r.in_force(at).bound("room_temp", scope)
    assert found is not None
    return found.low, found.high


# --- the shape ----------------------------------------------------------------------------


def test_what_an_intent_must_say() -> None:
    with pytest.raises(ValidationError, match="always has an end"):
        Intent(
            id="in-1",
            principal="user:anna",
            created=T0,
            scope=CS,
            kind="warmer",
            tier="temporary",
        )
    with pytest.raises(ValidationError, match="standing intent"):
        Intent(
            id="in-2", principal="u", created=T0, scope=CS, kind="comfort_band", tier="temporary"
        )
    with pytest.raises(ValidationError, match="above the high end"):
        Level(id="x", name="X", scope=CS, low=22, high=20)
    with pytest.raises(ValidationError, match="gives a time"):
        Validity(ends="at")


def test_the_shape_as_json() -> None:
    bath = kinds.bath(TANK, 50, local(21, 19, 30), principal="user:anna", created=T0)
    data = json.loads(bath.model_dump_json())
    assert data["kind"] == "bath"
    assert data["tier"] == "temporary"
    assert data["strength"] == "must"
    assert data["validity"]["ends"] == "when_met"
    assert data["expectations"][0]["targets"][0] == {
        "name": "tank_top_temp",
        "condition": "at_least",
        "low": None,
        "high": None,
        "value": 50.0,
        "level": None,
        "unit": "degC",
    }
    assert Intent.model_validate_json(bath.model_dump_json()) == bath
    Draft202012Validator(schema.generate()).validate(data)


def test_the_checked_in_schema_is_current() -> None:
    assert schema.shipped() == schema.dumps(schema.generate()), "run scripts/make-intentschema.py"


# --- what is in force ---------------------------------------------------------------------


def test_a_weekly_pattern_and_its_levels() -> None:
    r = resolver(band())
    assert low_high(r, local(21, 7)) == (20.5, 22.0)
    assert low_high(r, local(21, 23)) == (18.0, 20.0)
    assert low_high(r, local(22, 5, 59)) == (18.0, 20.0)  # past midnight
    found = r.in_force(local(21, 7)).bound("room_temp", CS)
    assert found is not None
    assert (found.low_rank, found.high_rank) == (2, 5)


def test_a_public_holiday_counts_as_sunday() -> None:
    weekdays = kinds.comfort_band(
        CS,
        [("day", (WEEKDAYS, time(6), time(22))), ("night", ((5, 6), time(0), time(0)))],
        principal="u",
        created=T0,
    )
    r = resolver(weekdays)
    assert low_high(r, local(24, 12)) == (20.5, 22.0)  # Christmas Eve: an ordinary Thursday
    assert low_high(r, local(25, 12)) == (18.0, 20.0)  # Christmas Day, a Friday: as Sunday


def test_seasons_and_outdoor_conditions() -> None:
    winter = kinds.comfort_band(CS, [("day", ((), None, None))], principal="u", created=T0)
    winter = winter.model_copy(
        update={
            "expectations": (
                winter.expectations[0].model_copy(
                    update={
                        "contexts": (
                            winter.expectations[0]
                            .contexts[0]
                            .model_copy(
                                update={"season": ("10-01", "04-30"), "outdoor_below": 5.0}
                            ),
                        )
                    }
                ),
            )
        }
    )
    r = resolver(winter)
    assert r.in_force(local(21, 12), outdoor=-3).bound("room_temp", CS) is not None
    assert r.in_force(local(21, 12), outdoor=8).bound("room_temp", CS) is None
    june = datetime(2026, 6, 21, 12, tzinfo=CAL.zone)
    assert r.in_force(june, outdoor=-3).bound("room_temp", CS) is None


def test_a_default_applies_only_where_nothing_is_set() -> None:
    seeded = kinds.comfort_band(
        CS, [("night", ((), None, None))], principal="seed", created=T0, confirmed=False
    )
    assert low_high(resolver(seeded), local(21, 12)) == (18.0, 20.0)
    assert low_high(resolver(seeded, band()), local(21, 12)) == (20.5, 22.0)


def test_warmer_is_an_offset_until_the_next_change() -> None:
    warmer = kinds.warmer(CS, 1.0, principal="user:bo", created=local(21, 8))
    r = resolver(band(), warmer)
    assert low_high(r, local(21, 9)) == (21.5, 23.0)
    assert r.end(r.intents[1]) == local(21, 22)  # the pattern goes to night at 22:00
    assert low_high(r, local(21, 22, 30)) == (18.0, 20.0)


def test_the_newest_temporary_intent_wins() -> None:
    warmer = kinds.warmer(CS, 1.0, principal="user:bo", created=local(21, 8))
    away = kinds.away(local(23, 18), {CS: "away"}, principal="user:anna", created=local(21, 9))
    r = resolver(band(), warmer, away)
    found = r.in_force(local(21, 10))
    assert low_high(r, local(21, 10)) == (15.0, 17.0)
    assert found.paused[warmer.id].startswith("shadowed by away (user:anna) until 2026-12-23 18:00")
    assert found.paused[r.intents[0].id].startswith("shadowed by away")
    # A warmer asked for after the away goes on top of the away's level.
    later = kinds.warmer(CS, 1.0, principal="user:bo", created=local(21, 11))
    assert low_high(resolver(band(), away, later), local(21, 12)) == (16.0, 18.0)


def test_comfort_waits_while_the_pump_has_heating_off() -> None:
    standing = band()
    found = resolver(standing).in_force(local(21, 12), heating=False)
    assert found.bound("room_temp", CS) is None
    assert found.paused[standing.id] == "the pump's heating stop has heating off"


def test_rooms_inherit_unless_they_have_their_own() -> None:
    rooms = {ROOM: CS, "room:living": CS}
    cool = kinds.comfort_band(ROOM, [("cool", ((), None, None))], principal="u", created=T0)
    r = resolver(band(), cool, rooms=rooms)
    assert low_high(r, local(21, 12), "room:living") == (20.5, 22.0)
    assert low_high(r, local(21, 12), ROOM) == (17.0, 19.0)


def test_floors_and_the_ranking() -> None:
    floor = kinds.hot_water_floor(TANK, 40.0, principal="u", created=T0)
    stance = kinds.cost_stance(
        ranking=["must_deadlines", "comfort_low", "should_deadlines", "comfort_high", "power_peak"],
        slider=0.3,
        principal="u",
        created=T0,
    )
    found = resolver(band(), floor, stance).in_force(local(21, 12))
    tank = found.bound("tank_top_temp", TANK)
    assert tank is not None
    assert (tank.low, tank.low_rank) == (40.0, 1)
    room = found.bound("room_temp", CS)
    assert room is not None
    assert room.low_rank == 3  # comfort's low edge moved below must deadlines
    assert found.slider == 0.3


def test_hands_off_fireplace_and_boost() -> None:
    off = kinds.hands_off(local(21, 20), principal="u", created=local(21, 8))
    fire = kinds.fireplace(CS, principal="u", created=local(21, 8))
    boost = kinds.boost_now(TANK, principal="u", created=local(21, 8))
    found = resolver(off, fire, boost).in_force(local(21, 9))
    assert found.hands_off_until == local(21, 20)
    assert found.fireplace == {CS}
    assert found.boost == (boost.id,)
    assert resolver(fire).in_force(local(21, 15)).fireplace == frozenset()  # 6 hours on


def test_deadlines_away_and_guests() -> None:
    morning = kinds.hot_water_by(TANK, [("tank", WEEKDAYS, time(7))], principal="u", created=T0)
    bath = kinds.bath(TANK, 52, local(22, 19, 30), principal="u", created=T0)
    r = resolver(morning, bath)
    found = r.deadlines(local(21, 8), local(24, 8))
    assert [(d.t, d.at_least, d.strength, d.rank) for d in found] == [
        (local(22, 7), 50.0, "should", 4),
        (local(22, 19, 30), 52.0, "must", 3),
        (local(23, 7), 50.0, "should", 4),
        (local(24, 7), 50.0, "should", 4),
    ]
    away = kinds.away(local(23, 18), {CS: "away"}, principal="u", created=local(21, 9))
    found = resolver(morning, away).deadlines(local(21, 8), local(24, 8))
    assert [(d.t, d.at_least) for d in found] == [(local(23, 18), 50.0), (local(24, 7), 50.0)]
    guests = kinds.guests(
        local(23, 0), {CS: "day"}, hot_water="tank-more", principal="u", created=local(21, 9)
    )
    found = resolver(morning, guests).deadlines(local(21, 8), local(24, 8))
    assert [d.at_least for d in found] == [55.0, 50.0, 50.0]  # the guests leave at midnight


# --- on the way in ------------------------------------------------------------------------

CAPS = Capabilities(
    systems=frozenset({CS}),
    rooms={ROOM: CS},
    offset=frozenset({CS}),
    tanks=frozenset({TANK}),
    block=frozenset({TANK}),
    emitters={CS: "floor"},
)


def test_a_contradiction_is_refused() -> None:
    other = kinds.comfort_band(CS, [("away", ((), None, None))], principal="u", created=T0)
    existing = [other.model_copy(update={"state": "active"})]
    verdict = check(band(), existing, LEVELS, CAPS, CAL, T0)
    assert not verdict.accepted
    assert verdict.intent.state == "rejected"
    assert "contradicts what is set for pump:hp1/cs1: room temp at least 20.5 and at most 17" in (
        verdict.intent.why or ""
    )


def test_what_is_refused_at_once() -> None:
    standing_mqtt = band().model_copy(update={"principal": "mqtt"})
    assert "MQTT" in (check(standing_mqtt, [], LEVELS, CAPS, CAL, T0).intent.why or "")
    peak = kinds.power_peak(10, principal="u", created=T0)
    assert "power reading" in (check(peak, [], LEVELS, CAPS, CAL, T0).intent.why or "")
    unknown = kinds.comfort_band(CS, [("nope", ((), None, None))], principal="u", created=T0)
    assert check(unknown, [], LEVELS, CAPS, CAL, T0).intent.why == "there is no level 'nope'"
    late = kinds.bath(
        TANK, 50, T0 - timedelta(hours=3), principal="u", created=T0 - timedelta(hours=4)
    )
    assert check(late, [], LEVELS, CAPS, CAL, T0).intent.why == "it would already have ended"


def test_the_answer_says_when_what_and_how_long() -> None:
    warmer = kinds.warmer(CS, 1.0, principal="user:bo", created=local(21, 8))
    existing = [band().model_copy(update={"state": "active"})]
    verdict = check(warmer, existing, LEVELS, CAPS, CAL, local(21, 8))
    assert verdict.accepted
    assert verdict.intent.state == "active"
    assert verdict.messages == (
        "Warmer, please (+1 °C): until the next change, 2026-12-21 22:00",
        "the floor heating will be 1 °C warmer in about 12 hours",
    )
    cooler = kinds.warmer(CS, -1.0, principal="user:bo", created=local(21, 8))
    assert check(cooler, existing, LEVELS, CAPS, CAL, local(21, 8)).messages == (
        "Cooler, please (-1 °C): until the next change, 2026-12-21 22:00",
        "the floor heating will be 1 °C cooler in about 12 hours",
    )
    away = kinds.away(local(23, 18), {CS: "away"}, principal="user:anna", created=local(21, 9))
    later = check(away, [*existing, verdict.intent], LEVELS, CAPS, CAL, local(21, 9))
    assert "Warmer, please (+1 °C) (user:bo) is set aside meanwhile" in later.messages


def test_hands_off_lasts_at_most_two_days() -> None:
    off = kinds.hands_off(local(21, 8) + timedelta(days=5), principal="u", created=local(21, 8))
    assert off.validity.at == local(23, 8)
    long = off.model_copy(update={"validity": Validity(ends="at", at=local(28, 8))})
    verdict = check(long, [], LEVELS, CAPS, CAL, local(21, 8))
    assert verdict.intent.validity.at == local(23, 8)
    assert "hands off lasts at most 48 hours" in verdict.messages


def test_what_the_house_lacks_is_said() -> None:
    bare = Capabilities(systems=frozenset({CS}), offset=frozenset({CS}))
    verdict = check(band(), [], LEVELS, bare, CAL, T0)
    assert verdict.accepted
    assert any("has no room sensor" in m for m in verdict.messages)
    morning = kinds.hot_water_by(TANK, [("tank", WEEKDAYS, time(7))], principal="u", created=T0)
    assert any("not held off" in m for m in check(morning, [], LEVELS, bare, CAL, T0).messages)


# --- kept ---------------------------------------------------------------------------------


@pytest.fixture
async def service(tmp_path: Path) -> AsyncIterator[Intents]:
    async with await Database.open(tmp_path / "db.sqlite") as db:
        await db.put(Location(latitude=57.7, longitude=12.0, timezone=ZONE))
        await db.put(Home(holidays="SE"))
        intents = Intents(db, AuditLog(tmp_path / "audit"), capabilities=lambda: CAPS)
        for level in LEVELS.values():
            await intents.put_level(level, who="admin", granted=frozenset({"*"}))
        yield intents


def audited(tmp_path: Path) -> list[dict[str, object]]:
    lines = (tmp_path / "audit" / "audit.jsonl").read_text().splitlines()
    return [json.loads(line) for line in lines]


HOUSEHOLD = frozenset({"intent.temporary.create", "intent.temporary.create.away", "plan.read"})


async def test_rights_decide_who_may_ask(service: Intents, tmp_path: Path) -> None:
    with pytest.raises(Forbidden, match=r"intent\.standing\.write"):
        await service.create(band(), granted=HOUSEHOLD, now=T0)
    off = kinds.hands_off(local(21, 20), principal="user:bo", created=local(21, 8))
    with pytest.raises(Forbidden, match=r"intent\.handsoff\.create"):
        await service.create(off, granted=HOUSEHOLD, now=local(21, 8))
    warmer = kinds.warmer(CS, 1.0, principal="user:bo", created=local(21, 8))
    assert (await service.create(warmer, granted=HOUSEHOLD, now=local(21, 8))).accepted
    assert [i.id for i in await service.all()] == [warmer.id]
    created = [e for e in audited(tmp_path) if e["what"] == "intent.create"]
    assert created[-1]["who"] == "user:bo"
    # Ending: one's own, or anyone's with the right.
    with pytest.raises(Forbidden, match=r"intent\.any\.end"):
        await service.end(warmer.id, who="user:anna", granted=HOUSEHOLD)
    ended = await service.end(warmer.id, who="user:bo", granted=HOUSEHOLD)
    assert ended.state == "finished"
    assert await service.all() == []


async def test_intents_move_along_with_time(service: Intents) -> None:
    admin = frozenset({"*"})
    later = kinds.warmer(CS, 1.0, principal="u", created=local(21, 8), until=local(21, 12))
    later = later.model_copy(
        update={"validity": later.validity.model_copy(update={"starts": local(21, 10)})}
    )
    bath = kinds.bath(TANK, 52, local(21, 19), principal="u", created=local(21, 8))
    for intent in (later, bath):
        await service.create(intent, granted=admin, now=local(21, 8))
    states = {i.id: i.state for i in await service.all()}
    assert states == {later.id: "scheduled", bath.id: "active"}
    await service.advance(local(21, 11))
    assert (await service.get(later.id)).state == "active"
    await service.advance(local(21, 21))
    assert (await service.get(later.id)).state == "finished"
    missed = await service.get(bath.id)
    assert (missed.state, missed.why) == ("missed", "not met by its latest")


def every_kind() -> list[Intent]:
    who: dict[str, Any] = {"principal": "user:anna", "created": T0}
    return [
        band(),
        kinds.no_sensor_band(CS, steps=3, **who),
        kinds.comfort_band(
            CS, [("day", ((0, 1), time(6), time(22)))], season=("10-01", "04-30"), **who
        ),
        kinds.hot_water_by(
            TANK, [("tank", (5, 6), time(7)), (52.0, (), time(19))], strength="must", **who
        ),
        kinds.hot_water_floor(TANK, 40.0, **who),
        kinds.cost_stance(ranking=tuple(reversed(RANKS)), slider=0.4, **who),
        kinds.addition_policy("limit", kw=3.0, **who),
        kinds.addition_policy("pump", **who),
        kinds.pool("pump:hp1/pool1", "day", pattern=[((5, 6), time(10), time(18))], **who),
        kinds.power_peak(9.0, pattern=[((), time(17), time(20))], **who),
        kinds.quiet_hours([((), time(22), time(7))], **who),
        kinds.warmer(CS, -1.5, until=local(21, 22), **who),
        kinds.warmer(CS, 1.0, **who),
        kinds.bath(TANK, 52, local(21, 19), strength="should", **who),
        kinds.guests(local(23, 18), {CS: "day"}, hot_water="tank-more", **who),
        kinds.away(local(27, 18), {CS: "away"}, **who),
        kinds.hands_off(local(22, 8), **who),
        kinds.fireplace(CS, **who),
        kinds.boost_now(TANK, **who),
    ]


def test_an_intent_put_as_a_request_builds_the_same_again() -> None:
    """What a form to change an intent starts from asks for the intent as it is."""
    for intent in every_kind():
        again = build(request_of(intent), principal=intent.principal, now=intent.created)
        assert again.model_copy(update={"id": intent.id}) == intent, intent.kind


async def test_changing_an_intent_in_place(service: Intents, tmp_path: Path) -> None:
    warmer = kinds.warmer(CS, 1.0, principal="user:bo", created=local(21, 8))
    await service.create(warmer, granted=HOUSEHOLD, now=local(21, 8))
    cooler = kinds.warmer(CS, -1.0, principal="user:bo", created=local(21, 8))
    # As ending: one's own, or anyone's with the right.
    with pytest.raises(Forbidden, match=r"intent\.any\.end"):
        await service.edit(warmer.id, cooler, who="user:anna", granted=HOUSEHOLD)
    verdict = await service.edit(
        warmer.id, cooler, who="user:bo", granted=HOUSEHOLD, now=local(21, 9)
    )
    assert verdict.accepted
    (kept,) = await service.all()
    assert (kept.id, kept.principal, kept.created) == (warmer.id, "user:bo", warmer.created)
    assert kept.parameters == {"offset": -1.0}
    edit = [e for e in audited(tmp_path) if e["what"] == "intent.edit"][-1]
    assert edit["who"] == "user:bo"
    details: Any = edit["details"]
    assert (details["before"]["parameters"], details["after"]["parameters"]) == (
        {"offset": 1.0},
        {"offset": -1.0},
    )
    # Its kind stays, and an ended one is asked for again instead.
    boost = kinds.boost_now(TANK, principal="user:bo", created=local(21, 8))
    with pytest.raises(ValueError, match="stays one"):
        await service.edit(warmer.id, boost, who="user:bo", granted=HOUSEHOLD)
    await service.end(warmer.id, who="user:bo", granted=HOUSEHOLD)
    with pytest.raises(ValueError, match="has ended"):
        await service.edit(warmer.id, cooler, who="user:bo", granted=HOUSEHOLD)


async def test_changing_a_seeded_intent_makes_it_the_households(service: Intents) -> None:
    seeded = kinds.no_sensor_band(CS, steps=2, principal="seed", created=T0, confirmed=False)
    await service.create(seeded, granted=frozenset({"*"}), now=T0)
    wider = kinds.no_sensor_band(CS, steps=3, principal="seed", created=T0)
    # Changing someone else's needs intent.any.end, and its new contents their own right.
    with pytest.raises(Forbidden, match=r"intent\.standing\.write"):
        await service.edit(
            seeded.id, wider, who="user:anna", granted=HOUSEHOLD | {"intent.any.end"}
        )
    admin = frozenset({"*"})
    assert (await service.edit(seeded.id, wider, who="user:anna", granted=admin, now=T0)).accepted
    kept = await service.get(seeded.id)
    assert (kept.confirmed, kept.tier, kept.principal) == (True, "standing", "seed")
    assert kept.expectations[0].targets[0].high == 3


async def test_a_level_in_use_stays(service: Intents) -> None:
    await service.create(band(), granted=frozenset({"*"}), now=T0)
    with pytest.raises(ValueError, match="is used by"):
        await service.delete_level("day", who="admin", granted=frozenset({"*"}))
    await service.delete_level("cool", who="admin", granted=frozenset({"*"}))
    assert "cool" not in await service.levels()


async def test_seeded_from_the_house_until_confirmed(service: Intents) -> None:
    found = Found(
        systems=(CS, "pump:hp1/cs2"),
        rooms={ROOM: CS, "room:living": CS},
        room_means={ROOM: 20.4, "room:living": 21.6},
        tanks={TANK: (41.3, 50.8)},
        addition=True,
    )
    seeded = await service.seed(found, T0)
    assert {(i.kind, i.scope, i.tier, i.confirmed) for i in seeded} == {
        ("comfort_band", CS, "default", False),
        ("comfort_band", "pump:hp1/cs2", "default", False),
        ("hot_water_floor", TANK, "protection", False),
        ("addition_policy", "house", "default", False),
    }
    levels = await service.levels()
    assert (levels["current-pump-hp1-cs1"].low, levels["current-pump-hp1-cs1"].high) == (20.5, 21.5)
    assert levels["current-pump-hp1-dhw"].top == 50.5
    no_sensor = next(i for i in seeded if i.scope == "pump:hp1/cs2")
    assert no_sensor.parameters == {"no_sensor": True}
    assert await service.seed(found, T0) == []  # once
    found_in_force = await service.in_force(local(21, 12))
    floor = found_in_force.bound("tank_top_temp", TANK)
    assert floor is not None
    assert floor.low == 41.0
    confirmed = await service.confirm(
        next(i.id for i in seeded if i.scope == CS), who="admin", granted=frozenset({"*"})
    )
    assert (confirmed.tier, confirmed.confirmed) == ("standing", True)


async def test_the_household_may_ask_for_things_for_a_while(tmp_path: Path) -> None:
    from thermaestro.auth import Accounts

    async with await Database.open(tmp_path / "db.sqlite") as db:
        granted = (await Accounts(db, AuditLog(tmp_path / "audit")).groups())["Household"]
    assert {
        "intent.temporary.create",
        "intent.temporary.create.away",
        "intent.temporary.create.guests",
        "plan.read",
    } <= granted
    assert "intent.standing.write" not in granted
