"""An installation's recording exported and replayed: a synthetic one, made from the
simulated house's tank, whose draws are known, so they serve as the labels."""

import gzip
import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from test_learn_tank import DAYS, recorded, truth

from thermaestro.cap.messages import Described
from thermaestro.cap.model import Delivery, Node, Point, Presence
from thermaestro.core.values import Key
from thermaestro.replay import export, replay
from thermaestro.replay.labels import Label, read_labels
from thermaestro.store import Database, Home, Location, Plugin

KEYS = {
    "top": Key("dev", "hp1/dhw/temp.top"),
    "low": Key("dev", "hp1/dhw/temp.charge"),
    "demand": Key("dev", "hp1/demand"),
}


def described() -> Described:
    every = Delivery(how="on_change")
    return Described(
        nodes=(
            Node(path="hp1", kind="unit", presence=Presence(how="configured")),
            Node(path="hp1/dhw", kind="dhw_tank", presence=Presence(how="configured")),
        ),
        points=tuple(Point(path=k.point, delivery=every) for k in KEYS.values()),
    )


async def recording(path: Path) -> Path:
    """The plant's tank, kept as an installation keeps it, and exported."""
    _, series = recorded()
    db_path = path / "installation.db"
    async with await Database.open(db_path) as db:
        await db.put(Location(latitude=59.3, longitude=18.1, timezone="UTC"))
        await db.put(Home(house="average"))
        await db.put(
            Plugin(plugin="nibe", settings={"host": "192.0.2.7", "model": "F1245"}), "pump"
        )
        body = described().model_dump_json(exclude={"id"})
        await db.run(
            lambda t: t.execute(
                "INSERT INTO described (instance, body, updated) VALUES ('dev', ?, 0)", (body,)
            )
        )
        await db.run(
            lambda t: t.executemany(
                "INSERT INTO history (instance, point, t, value, text, quality)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                [
                    (k.instance, k.point, s.t, s.value, s.text, s.quality)
                    for name, k in KEYS.items()
                    for s in series[name]
                ],
            )
        )
        out = path / "week.jsonl.gz"
        start, end = series["top"][0].t - 60, series["top"][-1].t + 60
        counts = await export(db, out, start, end)
    assert counts["described"] == 1
    assert counts["history"] == sum(len(series[n]) for n in KEYS)
    return out


def labels() -> list[Label]:
    scenario, _ = recorded()
    out = []
    for n in range(DAYS):
        day = date(2026, 1, 12) + timedelta(days=n)
        out += [
            Label(when.timestamp(), "draw", liters)
            for when, liters in truth(scenario, day)
            if liters >= 12
        ]
    return out


async def test_an_export_leaves_out_secrets_addresses_and_coordinates(tmp_path: Path) -> None:
    out = await recording(tmp_path)
    with gzip.open(out, "rt", encoding="utf-8") as f:
        text = f.read()
    assert "192.0.2.7" not in text  # the pump's address
    assert "59.3" not in text  # the location's coordinates
    assert "18.1" not in text
    header = json.loads(text.splitlines()[0])
    assert header["format"] == "thermaestro-export"
    assert header["timezone"] == "UTC"
    kinds = {json.loads(line)["record"] for line in text.splitlines()}
    assert {"header", "described", "setting", "history"} <= kinds


async def test_the_tank_is_learned_on_replay_and_its_draws_scored(tmp_path: Path) -> None:
    out = await recording(tmp_path)
    summary = await replay(out, tmp_path / "replayed", labels=labels())
    # Every whole day but the last, which is read an hour after it ends: not recorded.
    assert len(summary["days"]) == DAYS - 1
    assert summary["coverage"]["dev:hp1/dhw/temp.top"]["samples"] > 0
    card = summary["scorecard"]
    assert card["found"] >= 0.8 * (card["found"] + card["missed"])
    inputs = json.loads((tmp_path / "replayed" / "inputs.json").read_text())
    assert {i["point"]: i["verdict"] for i in inputs}["hp1/dhw/temp.top"] == "usable"
    learned = json.loads((tmp_path / "replayed" / "learned.json").read_text())["replayed"]
    assert "tank.demand@dev:hp1/dhw" in learned


async def test_replaying_fewer_days_gives_the_same_for_those_days(tmp_path: Path) -> None:
    """Causal: nothing learned on a day depends on what came after it."""
    out = await recording(tmp_path)
    await replay(out, tmp_path / "all")
    await replay(out, tmp_path / "three", days=3)

    def lines(path: Path) -> list[dict[str, object]]:
        found = []
        for line in path.read_text().splitlines():
            row = json.loads(line)
            found.append(row)
        return found

    every = lines(tmp_path / "all" / "learned.jsonl")
    first = lines(tmp_path / "three" / "learned.jsonl")
    days = sorted({r["day"] for r in first})  # type: ignore[type-var]
    assert len(days) == 3
    assert [r for r in every if r["day"] in days] == first


def test_labels_are_read_in_the_houses_time(tmp_path: Path) -> None:
    path = tmp_path / "labels.csv"
    path.write_text("time,what,liters\n2026-10-14 07:15,shower,40\n2026-10-14 21:00,bath,\n")
    found = read_labels(path, UTC)
    assert found[0] == Label(datetime(2026, 10, 14, 7, 15, tzinfo=UTC).timestamp(), "shower", 40.0)
    assert found[1].liters is None
    bad = tmp_path / "bad.csv"
    bad.write_text("time,what\nyesterday,shower\n")
    with pytest.raises(ValueError, match="a time such as"):
        read_labels(bad, UTC)


async def test_the_replay_command(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    import asyncio

    from thermaestro.cli import main

    out = await recording(tmp_path)
    labels = tmp_path / "labels.csv"
    labels.write_text("time,what\n2026-01-13 06:30,shower\n")
    code = await asyncio.to_thread(
        main, ["replay", str(out), str(tmp_path / "r"), "--labels", str(labels), "--days", "2"]
    )
    assert code == 0
    said = capsys.readouterr().out
    assert "replayed 2 days" in said
    assert "labels: 1 found, 0 missed" in said
    assert await asyncio.to_thread(main, ["replay", str(labels), str(tmp_path / "x")]) == 2
