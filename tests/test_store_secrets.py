import json
import logging
import stat
from pathlib import Path

import pytest
from pydantic import SecretStr

from thermaestro.store import Mqtt, SecretStore, StoreError

PASSWORD = "correct-horse-battery-staple"


def mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


async def test_set_get_delete(tmp_path: Path) -> None:
    store = SecretStore(tmp_path / "secrets.json")
    assert await store.get("mqtt.password") is None
    await store.set("mqtt.password", PASSWORD)
    got = await store.get("mqtt.password")
    assert got is not None
    assert got.get_secret_value() == PASSWORD
    assert await store.names() == ["mqtt.password"]
    assert await store.delete("mqtt.password")
    assert await store.get("mqtt.password") is None


async def test_it_survives_a_restart(tmp_path: Path) -> None:
    await SecretStore(tmp_path / "secrets.json").set("tibber.token", PASSWORD)
    again = await SecretStore(tmp_path / "secrets.json").get("tibber.token")
    assert again == SecretStr(PASSWORD)


async def test_the_file_is_this_users_only(tmp_path: Path) -> None:
    path = tmp_path / "secrets.json"
    await SecretStore(path).set("a", PASSWORD)
    assert mode(path) == 0o600
    path.chmod(0o644)
    with pytest.raises(StoreError, match="open to others"):
        await SecretStore(path).get("a")


async def test_a_crash_between_write_and_rename_leaves_the_old_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "secrets.json"
    store = SecretStore(path)
    await store.set("a", "old")

    def crash(self: Path, target: Path) -> Path:
        raise OSError("power lost")

    monkeypatch.setattr(Path, "replace", crash)
    with pytest.raises(OSError, match="power lost"):
        await store.set("a", "new")
    monkeypatch.undo()
    assert json.loads(path.read_text())["secrets"] == {"a": "old"}
    assert [p.name for p in tmp_path.iterdir()] == ["secrets.json"]  # no temporary left
    assert await SecretStore(path).get("a") == SecretStr("old")


async def test_rotation_stores_the_new_value_before_the_old_is_given_up(tmp_path: Path) -> None:
    store = SecretStore(tmp_path / "secrets.json")
    await store.set("tibber.refresh", "r1")
    assert await store.replace("tibber.refresh", "r2", old=SecretStr("r1"))
    on_disk = json.loads((tmp_path / "secrets.json").read_text())["secrets"]
    assert on_disk == {"tibber.refresh": "r2"}
    # A second rotation from the same old value lost the race and changes nothing.
    assert not await store.replace("tibber.refresh", "r3", old=SecretStr("r1"))
    assert await store.get("tibber.refresh") == SecretStr("r2")


async def test_secrets_never_appear_in_a_dump_or_a_log_line(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    store = SecretStore(tmp_path / "secrets.json")
    await store.set("mqtt.password", PASSWORD)
    await store.replace("mqtt.password", PASSWORD[::-1], old=SecretStr(PASSWORD))
    secret = await store.get("mqtt.password")
    settings = Mqtt(host="broker.example", username="thermaestro", password="mqtt.password")
    logging.getLogger("test").info(
        "store %r, secret %s %r, settings %s", store, secret, secret, settings
    )
    shown = caplog.text + repr(store) + repr(secret) + settings.model_dump_json()
    assert PASSWORD not in shown
    assert PASSWORD[::-1] not in shown
    assert "mqtt.password" in caplog.text  # names are fine


async def test_a_broken_file_is_reported_without_quoting_it(tmp_path: Path) -> None:
    path = tmp_path / "secrets.json"
    path.write_text('{"format": 1, "secrets": {"a": "' + PASSWORD + '"')
    path.chmod(0o600)
    with pytest.raises(StoreError) as e:
        await SecretStore(path).get("a")
    assert PASSWORD not in str(e.value)


async def test_a_bad_name_is_refused(tmp_path: Path) -> None:
    with pytest.raises(StoreError, match="isn't a secret's name"):
        await SecretStore(tmp_path / "secrets.json").set("Has Spaces", PASSWORD)
