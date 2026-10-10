"""Requests over MQTT: asked as the MQTT principal, with the MQTT group's rights only, and
only for a while."""

from pathlib import Path

from thermaestro.core import AuditLog
from thermaestro.core.daemon import _mqtt_asker
from thermaestro.intents import Capabilities, Intents
from thermaestro.store import Database

CS = "pump:hp1/cs1"


async def grant(db: Database, right: str) -> None:
    await db.run(
        lambda t: t.execute(
            "INSERT INTO group_permissions (group_name, permission) VALUES ('MQTT', ?)", (right,)
        )
    )


async def test_requests_over_mqtt_have_the_mqtt_groups_rights(tmp_path: Path) -> None:
    async with await Database.open(tmp_path / "t.db") as db:
        caps = Capabilities(systems=frozenset({CS}), offset=frozenset({CS}))
        intents = Intents(db, AuditLog(tmp_path / "audit"), capabilities=lambda: caps)
        ask = _mqtt_asker(db, intents)
        warmer = {"kind": "warmer", "scope": CS, "offset": 1}
        refused = await ask(warmer)
        assert refused == {
            "accepted": False,
            "messages": ["the MQTT group needs the right intent.temporary.create"],
        }
        await grant(db, "intent.temporary.create")
        accepted = await ask(warmer)
        assert accepted["accepted"]
        assert [(i.kind, i.principal) for i in await intents.all()] == [("warmer", "mqtt")]
        await grant(db, "*")
        standing = await ask({"kind": "cost_stance", "slider": 1})
        assert not standing["accepted"]
        assert standing["messages"][0].startswith("a request over MQTT is for a while")
        nonsense = await ask({"kind": "nonsense"})
        assert not nonsense["accepted"]
        extra = await ask({"kind": "boost_now", "offset": 1})
        assert extra["messages"] == ["boost_now doesn't take offset"]
