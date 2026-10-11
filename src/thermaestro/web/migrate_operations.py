"""Coming from NibePi: its config.json read into a draft, kept a while for the household to
check, and the parts it ticks made through the same operations setup uses. Nothing is
made until then, and nothing is written to the pump.

Then, once the household says NibePi is stopped, the review of the pump's own settings:
the registers NibePi changes, read from the pump in the background, and on request every
writable register beside its factory default. A change is a person's, through the
executor."""

import asyncio
import functools
import secrets
import time
from collections.abc import Collection
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from ..auth import AccountError, Forbidden
from ..cap import Closed
from ..cap.client import CapError
from ..core.executor import TAKEN
from ..migrate import review
from ..migrate.nibepi import LOCALHOSTS, Draft, NotNibePi, read
from ..nibe.maps import ModelMap, RegisterMap, load
from ..spotsources import choices
from ..store import NibePiReview, Plugin, PriceLayer

if TYPE_CHECKING:
    from ..core import Executor, PluginHost
    from ..core.audit import AuditLog
    from ..store import Database
    from .operations import Caller

DRAFT_S = 1800.0
"""How long a draft is kept for confirming."""
BROKER_WAIT_S = 2.0
READ_S = 15.0
"""The longest wait for one register: a read waits its turn on the bus."""
REVIEWED = "the review of the pump's settings after NibePi"


@dataclass
class Reading:
    """Registers being read from the pump in the background, and what came back."""

    registers: list[int]
    seen: dict[int, review.Seen] = field(default_factory=dict)
    task: "asyncio.Task[None] | None" = None

    @property
    def finished(self) -> bool:
        return self.task is None or self.task.done()


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
        host: PluginHost | None
        executor: Executor | None
        audit: AuditLog
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
        async def write_device(
            self, caller: Caller, instance: str, point: str, value: float, why: str | None = None
        ) -> dict[str, Any]: ...
        async def set_baseline(self, caller: Caller, ref: str, value: Any) -> None: ...

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
        pump = draft.item("pump:pump")
        kept_review = await self.db.get(NibePiReview)
        await self.db.put(
            NibePiReview(
                pump=pump.id if pump is not None and pump in picked else self._nibe_instance(),
                before=draft.before,
                offsets=draft.offsets,
                stopped=kept_review.stopped if kept_review is not None else None,
            )
        )
        del self._kept()[token]
        return {"done": done, "problems": problems}

    # --- the review of the pump's settings ------------------------------------------------

    def _nibe_instance(self) -> str:
        """The first Nibe pump's instance, or `pump`."""
        host = self.host
        for id, instance in sorted((host.instances if host else {}).items()):
            if isinstance(instance.setting, Plugin) and instance.setting.plugin == "nibe":
                return id
        return "pump"

    def _readings(self) -> dict[str, Reading]:
        if not hasattr(self, "_readings_kept"):
            self._readings_kept: dict[str, Reading] = {}
        return self._readings_kept

    async def _reviewed_pump(self) -> tuple[str, ModelMap | None, str | None]:
        """The pump the review is of: its instance, its register map, and why there is no
        review, where there isn't."""
        setting = await self.db.get(NibePiReview)
        id = setting.pump if setting is not None else self._nibe_instance()
        instance = (self.host.instances if self.host else {}).get(id)
        if instance is None or instance.described is None:
            return id, None, "the pump isn't connected"
        unit = next((n for n in instance.described.nodes if n.kind == "unit"), None)
        identity = unit.identity if unit is not None else None
        if identity is None or identity.vendor != "Nibe" or not identity.map:
            return id, None, "this isn't a Nibe pump"
        if not identity.map.startswith("nibe-bus-"):
            return id, None, "changing an S-series pump's settings isn't built yet"
        try:
            return id, _bus().model(identity.model or ""), None
        except (KeyError, ValueError):
            return id, None, f"the register map has no {identity.model}"

    def _levers_by_register(self, id: str) -> dict[int, str]:
        instance = (self.host.instances if self.host else {}).get(id)
        if instance is None or instance.described is None:
            return {}
        return review.touched(
            (f"{id}:{lv.path}", lv.touches)
            for lv in instance.described.levers
            if lv.kind == "setting" and lv.unavailable is None
        )

    async def nibepi_review(self, caller: "Caller") -> dict[str, Any]:
        """The review of the pump's settings: whether NibePi is said to be stopped, the
        listed registers with what is known of each, and the full comparison so far."""
        self._require(caller, "settings.read")
        setting = await self.db.get(NibePiReview) or NibePiReview()
        id, model, why = await self._reviewed_pump()
        out: dict[str, Any] = {
            "pump": id,
            "why": why,
            "stopped": setting.stopped,
            "rows": [],
            "reading": None,
            "compared": None,
            "levers": {},
        }
        if model is None:
            return out
        levers = self._levers_by_register(id)
        listed = self._readings().get("list")
        everything = self._readings().get("all")
        out["rows"] = review.review(
            model,
            listed.seen if listed else {},
            before=setting.before,
            usual=review.offsets_by_register(setting.offsets),
            levers=levers,
        )
        if listed is not None:
            out["reading"] = _progress(listed)
        if everything is not None:
            out["compared"] = {
                **_progress(everything),
                "unread": sum(1 for s in everything.seen.values() if s.why),
                "rows": review.compared(model, everything.seen, levers),
            }
        executor = self.executor
        for ref in set(levers.values()):
            lever = executor.lever(ref) if executor else None
            claim = executor.claims.get(ref) if executor else None
            param = lever.params.get("value") if lever else None
            names = param.enum.value if param is not None and param.enum.trusted else None
            out["levers"][ref] = {
                "baseline": claim.baseline if claim else None,
                "choices": list(names) if names else None,
            }
        return out

    async def nibepi_stopped(self, caller: "Caller") -> None:
        """The household says NibePi is stopped: its values are final, and can be reviewed."""
        self._require(caller, "settings.write")
        setting = await self.db.get(NibePiReview) or NibePiReview(pump=self._nibe_instance())
        await self.db.put(setting.model_copy(update={"stopped": datetime.now(UTC)}))
        await self.audit.record(
            caller.principal.name, "nibepi_import.stopped", source=caller.source
        )

    async def read_pump_settings(self, caller: "Caller", *, everything: bool = False) -> None:
        """Read the listed registers from the pump in the background; with `everything`,
        every register of the full comparison (about a second each)."""
        self._require(caller, "settings.write")
        setting = await self.db.get(NibePiReview)
        if setting is None or setting.stopped is None:
            raise AccountError("first say that NibePi is stopped: while it runs, it keeps writing")
        id, model, why = await self._reviewed_pump()
        if model is None:
            raise AccountError(why or "no pump to review")
        key = "all" if everything else "list"
        running = self._readings().get(key)
        if running is not None and not running.finished:
            return
        registers = review.comparable(model) if everything else review.listed(model)
        reading = Reading(registers)
        reading.task = asyncio.create_task(self._read_into(id, reading))
        self._readings()[key] = reading

    async def _read_into(self, id: str, reading: Reading) -> None:
        for register in reading.registers:
            reading.seen[register] = await self._read_register(id, register)

    async def _read_register(self, id: str, register: int) -> review.Seen:
        instance = (self.host.instances if self.host else {}).get(id)
        link = instance.link if instance is not None else None
        if link is None:
            return review.Seen(None, None, "the pump isn't connected")
        try:
            answer = await link.read(
                [review.point(register)], after=datetime.now(UTC), timeout=READ_S
            )
        except (TimeoutError, CapError, Closed):
            return review.Seen(None, None, "no answer")
        envelope = answer.values[0]
        if envelope.quality != "good":
            return review.Seen(None, None, envelope.why or envelope.quality)
        raw = envelope.raw if isinstance(envelope.raw, int) else None
        return review.Seen(envelope.value, raw)

    async def review_write(self, caller: "Caller", register: int, value: float) -> None:
        """Change one of the pump's own settings from the review, then read it again."""
        id, model, why = await self._reviewed_pump()
        if model is None:
            raise AccountError(why or "no pump to review")
        if register not in model:
            raise AccountError(f"the pump has no register {register}")
        result = await self.write_device(caller, id, review.point(register), value, REVIEWED)
        seen = await self._read_register(id, register)
        for reading in self._readings().values():
            if register in reading.registers:
                reading.seen[register] = seen
        if result["outcome"] not in TAKEN:
            detail = f": {result['detail']}" if result["detail"] else ""
            raise AccountError(f"{register} wasn't changed ({result['outcome']}){detail}")


@functools.cache
def _bus() -> RegisterMap:
    return load("bus")


def _progress(reading: Reading) -> dict[str, Any]:
    return {
        "done": len(reading.seen),
        "total": len(reading.registers),
        "finished": reading.finished,
    }


async def _answers(host: str, port: int) -> bool:
    """Whether something listens on the port: a broker, most likely."""
    try:
        _, writer = await asyncio.wait_for(asyncio.open_connection(host, port), BROKER_WAIT_S)
    except (OSError, TimeoutError):
        return False
    writer.close()
    return True
