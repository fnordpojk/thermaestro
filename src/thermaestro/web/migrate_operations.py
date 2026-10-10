"""Coming from NibePi: its config.json read into a draft, kept a while for the household to
check, and the parts it ticks made through the same operations setup uses. Nothing is
made until then, and nothing is written to the pump."""

import asyncio
import secrets
import time
from collections.abc import Collection
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from ..auth import AccountError, Forbidden
from ..migrate.nibepi import LOCALHOSTS, Draft, NotNibePi, read
from ..spotsources import choices
from ..store import PriceLayer

if TYPE_CHECKING:
    from ..store import Database
    from .operations import Caller

DRAFT_S = 1800.0
"""How long a draft is kept for confirming."""
BROKER_WAIT_S = 2.0


@dataclass
class Kept:
    draft: Draft
    user: str
    made: float
    broker: bool | None
    """Whether a broker answered on this host, where NibePi's was on its own."""


class MigrateOperations:
    """Mixed into the services, whose parts it uses."""

    if TYPE_CHECKING:
        db: Database
        _drafts: dict[str, Kept]

        def _require(self, caller: Caller, permission: str, *, step_up: bool = False) -> None: ...

        async def set_location(self, caller: Caller, body: dict[str, Any]) -> Any: ...
        async def set_secret(self, caller: Caller, name: str, value: str) -> None: ...
        async def set_mqtt(self, caller: Caller, body: dict[str, Any]) -> Any: ...
        async def set_discovery(self, caller: Caller, body: dict[str, Any]) -> Any: ...
        async def set_pump(self, caller: Caller, id: str, body: dict[str, Any]) -> Any: ...
        async def set_room(self, caller: Caller, id: str | None, body: dict[str, Any]) -> Any: ...
        async def set_sensor(self, caller: Caller, id: str | None, body: dict[str, Any]) -> Any: ...
        async def set_price_source(
            self, caller: Caller, plugin: str, id: str, settings: dict[str, Any]
        ) -> Any: ...
        async def choose_spot(
            self, caller: Caller, zone: str, source: str, fallback: str | None = None
        ) -> Any: ...
        async def set_price_layer(
            self, caller: Caller, id: str | None, body: dict[str, Any]
        ) -> Any: ...
        async def set_vat(self, caller: Caller, body: dict[str, Any]) -> Any: ...
        async def price_sources(self, caller: Caller) -> dict[str, dict[str, Any]]: ...

    def _kept(self) -> dict[str, Kept]:
        if not hasattr(self, "_drafts"):
            self._drafts = {}
        now = time.monotonic()
        for token in [t for t, k in self._drafts.items() if now - k.made > DRAFT_S]:
            del self._drafts[token]
        return self._drafts

    async def read_nibepi(self, caller: "Caller", text: str) -> tuple[str, Kept]:
        """Read NibePi's config.json into a draft, kept for this user for half an hour."""
        self._require(caller, "settings.write")
        try:
            draft = read(text)
        except NotNibePi as e:
            raise AccountError(str(e)) from None
        broker = None
        mqtt = next((i for i in draft.items if i.kind == "mqtt"), None)
        if mqtt is not None and mqtt.body.get("host") in LOCALHOSTS:
            broker = await _answers("127.0.0.1", int(mqtt.body.get("port", 1883)))
        token = secrets.token_urlsafe(16)
        kept = Kept(draft, caller.principal.name, time.monotonic(), broker)
        self._kept()[token] = kept
        return token, kept

    def nibepi_draft(self, caller: "Caller", token: str) -> Kept:
        self._require(caller, "settings.write")
        kept = self._kept().get(token)
        if kept is None or kept.user != caller.principal.name:
            raise AccountError("that draft is gone: read the file again")
        return kept

    async def apply_nibepi(
        self,
        caller: "Caller",
        token: str,
        chosen: Collection[str],
        *,
        timezone: str | None = None,
        model: str | None = None,
    ) -> dict[str, list[str]]:
        """Make the ticked parts of a draft (`<kind>:<id>`), each as setup would; one that is
        refused doesn't stop the others. The password is asked again first, since the pump
        and secrets are among them."""
        kept = self.nibepi_draft(caller, token)
        self._require(caller, "plugins.manage", step_up=True)
        draft = kept.draft
        picked = [i for i in draft.items if f"{i.kind}:{i.id}" in set(chosen)]
        kinds = {i.kind for i in picked}
        done: list[str] = []
        problems: list[str] = []
        rooms = {i.id for i in picked if i.kind == "room"}
        made_rooms: dict[str, str] = {}

        async def attempt(what: str, work: Any) -> Any:
            try:
                result = await work
            except (AccountError, Forbidden) as e:
                problems.append(f"{what}: {e}")
                return None
            done.append(what)
            return result

        for name, value in draft.secrets.items():
            needed = "mqtt" if name.startswith("mqtt.") else "tibber"
            if needed in kinds:
                await attempt(f"the secret {name}", self.set_secret(caller, name, value))
        for item in picked:
            body = dict(item.body)
            match item.kind:
                case "location":
                    if timezone:
                        body["timezone"] = timezone
                    await attempt(item.what, self.set_location(caller, body))
                case "mqtt":
                    await attempt(item.what, self.set_mqtt(caller, body))
                case "discovery":
                    await attempt(item.what, self.set_discovery(caller, body))
                case "pump":
                    if model and body.get("protocol") == "modbus-tcp":
                        body["model"] = model
                    await attempt(item.what, self.set_pump(caller, item.id, body))
                case "room":
                    made = await attempt(item.what, self.set_room(caller, None, body))
                    if made is not None:
                        made_rooms[item.id] = made[0]
                case "tibber":
                    await attempt(item.what, self.set_price_source(caller, "tibber", item.id, body))
        for item in picked:
            if item.kind == "sensor":
                body = dict(item.body)
                room = body.pop("room", None)
                if room in rooms and room in made_rooms:
                    body["room"] = made_rooms[room]
                await attempt(item.what, self.set_sensor(caller, None, body))
        for item in picked:
            if item.kind == "spot":
                has = {p["plugin"] for p in (await self.price_sources(caller)).values()}
                offered = choices(item.id, tibber="tibber" in has, entsoe="entsoe" in has)
                if not offered:
                    problems.append(f"{item.what}: no source offers it")
                    continue
                fallback = offered[1].plugin if len(offered) > 1 else None
                await attempt(
                    item.what, self.choose_spot(caller, item.id, offered[0].plugin, fallback)
                )
        layers = await self.db.all(PriceLayer)
        for item in picked:
            if item.kind == "layer":
                if any(x.role == item.body["role"] for x in layers.values()):
                    problems.append(f"{item.what}: the stack has a layer for it already")
                    continue
                await attempt(item.what, self.set_price_layer(caller, None, dict(item.body)))
        for item in picked:
            if item.kind == "vat":
                ids = sorted(await self.db.all(PriceLayer))
                await attempt(
                    item.what, self.set_vat(caller, {"rate": item.body["rate"], "applies_to": ids})
                )
        del self._kept()[token]
        return {"done": done, "problems": problems}


async def _answers(host: str, port: int) -> bool:
    """Whether something listens on the port: a broker, most likely."""
    try:
        _, writer = await asyncio.wait_for(asyncio.open_connection(host, port), BROKER_WAIT_S)
    except (OSError, TimeoutError):
        return False
    writer.close()
    return True
