"""The Nibe plugin for the bus family (F-series, VVM, SMO, MHB), read-only for now.

It connects to the pump's gateway, identifies the pump (its model from the product
information it sends every 15 s, its firmware from 43001/44331, the word order of 32-bit
values from 48852), finds the climate systems it has, and then keeps every point fresh:
registers the pump pushes (its LOG.SET) as they come, the rest polled one at a time.

Nothing here writes to the pump: an `act` is dropped as read-only.
"""

import asyncio
import contextlib
import logging
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from ..cap import Message, Send
from ..cap.messages import (
    Act,
    Describe,
    Described,
    Error,
    Fate,
    Health,
    Read,
    Subscribe,
    Update,
    Values,
)
from ..cap.model import (
    Ack,
    Delivery,
    Envelope,
    Identity,
    Knowledge,
    Node,
    Point,
    Presence,
    Promises,
    Quality,
    Range,
)
from ..cap.vocabulary import point as standard_point
from ..core.plugins import PluginContext
from ..store import NibeGateway, SecretStore
from . import profile
from .maps import ModelMap, Register, RegisterMap, Status, decode, load, words
from .transport import GatewayConfig, connect
from .transport.base import FateKind, Observed, ReadFailed, Transport
from .transport.nibegw import PlainSettings

log = logging.getLogger(__name__)

PRODUCT_INFO = 0x6D
LOG_SET = 0x68
READ_TIMEOUT_S = 5.0
PUSHED_FRESHNESS_S = 30.0
POLLED_FRESHNESS_S = 900.0
COUNTERS = ("heat.produced", "elec.used")
"""Points that only count up, and start over from 0 at their register's size."""


@dataclass(frozen=True, slots=True)
class Sample:
    words: tuple[int, int]
    t: float
    """Monotonic seconds."""


class NotIdentified(Exception):
    pass


def model_for(product: str, maps: RegisterMap) -> str | None:
    """The register map's model a product name stands for: `F1245-6 CU` → `F1245`."""
    squeezed = re.sub(r"\s+", "", product)
    matches = [m for m in maps.models if squeezed.upper().startswith(m.upper())]
    return max(matches, key=len) if matches else None


class NibePlugin:
    name = "nibe"
    version = "0.1.0"
    features: tuple[str, ...] = ("subscribe",)

    def __init__(
        self,
        gateway: NibeGateway,
        *,
        secrets: SecretStore | None = None,
        maps: RegisterMap | None = None,
        connect_fn: Callable[..., Awaitable[Transport]] = connect,
        transport_settings: dict[str, Any] | None = None,
        identify_timeout_s: float = 40.0,
        health_interval_s: float = 10.0,
        read_timeout_s: float = READ_TIMEOUT_S,
    ) -> None:
        self.gateway = gateway
        self._secrets = secrets
        self.maps = maps or load("bus")
        self._connect = connect_fn
        self._transport_settings = transport_settings or {}
        self._identify_timeout = identify_timeout_s
        self._health_interval = health_interval_s
        self._read_timeout = read_timeout_s
        self._transport: Transport | None = None
        self._identified: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        # A failure is also reported by `events`; nobody may be waiting to retrieve it here.
        self._identified.add_done_callback(lambda f: f.cancelled() or f.exception())
        self._product: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        self.model: ModelMap | None = None
        self.firmware: str | None = None
        self.high_word_first: bool | None = None
        self.layout: profile.Layout | None = None
        self._samples: dict[int, Sample] = {}
        self.pushed: set[int] = set()
        self.absent: set[int] = set()
        """Registers that gave no value, whose points were removed."""
        self._tick = asyncio.Event()
        self._read_lock = asyncio.Lock()
        self._charge_started: float | None = None

    # --- the plugin interface --------------------------------------------------------------

    async def events(self, send: Send) -> None:
        if self._transport is not None or self._identified.done():
            raise RuntimeError("a Nibe plugin instance serves one connection")
        try:
            self._transport = await self._connect_transport()
        except Exception as e:
            await send(Health(t=_now(), unit=profile.UNIT, state="down", needs_user_action=None))
            self._identified.set_exception(NotIdentified(f"can't reach the gateway: {e}"))
            raise
        stop = self._transport.observe(self._observed)
        poller: asyncio.Task[None] | None = None
        try:
            await self._identify()
            self._identified.set_result(None)
            poller = asyncio.create_task(self._poll(send))
            while True:
                await send(self._health())
                await asyncio.sleep(self._health_interval)
        except Exception as e:
            if not self._identified.done():
                self._identified.set_exception(NotIdentified(str(e)))
            raise
        finally:
            stop()
            if poller is not None:
                poller.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await poller
            await self._transport.close()

    async def handle(self, request: Message, send: Send) -> None:
        if isinstance(request, Act):
            await send(
                Fate(id=request.id, stage="dropped", t=_now(), detail="read-only in this version")
            )
            return
        if not isinstance(request, Describe | Read | Subscribe):
            await send(
                Error(id=getattr(request, "id", None), code="unsupported", detail="not offered")
            )
            return
        await asyncio.shield(self._identified)
        if isinstance(request, Describe):
            await send(self.describe(request.id))
        elif isinstance(request, Read):
            await send(Values(id=request.id, values=tuple(await self._read_points(request))))
        else:
            await self._subscription(request, send)

    # --- connecting and identifying ---------------------------------------------------------

    async def _connect_transport(self) -> Transport:
        psk = None
        if self.gateway.protocol == "thermaestro-gw" and self.gateway.psk and self._secrets:
            secret = await self._secrets.get(self.gateway.psk)
            if secret is None:
                raise NotIdentified(f"the gateway key {self.gateway.psk!r} isn't in the secrets")
            psk = bytes.fromhex(secret.get_secret_value())
        config = GatewayConfig(
            host=self.gateway.host,
            read_port=self.gateway.read_port,
            write_port=self.gateway.write_port,
            control_port=self.gateway.control_port
            if self.gateway.protocol == "thermaestro-gw"
            else None,
            psk=psk,
        )
        settings = dict(self._transport_settings)
        settings.setdefault("plain_settings", PlainSettings(local_port=self.gateway.local_port))
        return await self._connect(config, **settings)

    async def _identify(self) -> None:
        # Read first: a plain NibeGW gateway forwards the bus (and with it the product
        # information) only to clients that have sent it something. These registers are the
        # same on every bus-family model: 43001 u16, 44331 u8, 48852 u8.
        version = await self._read_word(profile.FIRMWARE[0])
        release = await self._read_word(profile.FIRMWARE[1])
        if version is not None:
            self.firmware = f"{version}R{release & 0xFF}" if release is not None else str(version)
        swap = await self._read_word(profile.WORD_SWAP)
        self.high_word_first = None if swap is None else swap & 0xFF == 0
        name = self.gateway.model
        if name is None:
            product = await asyncio.wait_for(asyncio.shield(self._product), self._identify_timeout)
            name = model_for(product, self.maps)
            if name is None:
                raise NotIdentified(f"no register map for the product {product!r}; set the model")
        self.model = self.maps.model(name)
        systems = [1]
        for system in profile.detectable(self.model):
            if system.accessory is None or await self._read_value(system.accessory) != 1:
                continue
            words_ = await self._read(system.supply)
            if words_ is not None:
                supply = decode(self.model.register(system.supply), *words_, high_word_first=True)
                if supply.status is not Status.NOT_CONNECTED:
                    systems.append(system.number)
        self.layout = profile.layout(self.model, systems)
        log.info("pump %s, firmware %s, climate systems %s", name, self.firmware, systems)

    # --- reading ---------------------------------------------------------------------------

    def _observed(self, observed: Observed) -> None:
        telegram = observed.telegram
        if telegram is None:
            return
        if telegram.command == PRODUCT_INFO and not self._product.done():
            self._product.set_result(telegram.payload[3:].decode("ascii", errors="replace"))
        elif telegram.command == LOG_SET:
            payload = telegram.payload
            for i in range(0, len(payload) - 3, 4):
                register = int.from_bytes(payload[i : i + 2], "little")
                if register == 0xFFFF:
                    continue  # an empty slot
                self.pushed.add(register)
                self._store(register, (int.from_bytes(payload[i + 2 : i + 4], "little"), 0))

    async def _read(self, register: int, after: float | None = None) -> tuple[int, int] | None:
        if self._transport is None:
            return None
        async with self._read_lock:
            try:
                reading = await self._transport.read(
                    register, after=after, timeout=self._read_timeout
                )
            except ReadFailed as e:
                log.debug("read %d failed: %s", register, e.why)
                return None
        first, second = words(reading.data)
        self._store(register, (first, second))
        following = register + 1
        if self.model is not None and following in self.model and following not in self.pushed:
            size = self.model.register(following).size
            if size is not None and size.bits <= 16:
                self._store(following, (second, 0))  # the answer carries the next one too
        return first, second

    async def _read_word(self, register: int) -> int | None:
        words_ = await self._read(register)
        return None if words_ is None else words_[0]

    async def _read_value(self, register: int) -> float | int | None:
        words_ = await self._read(register)
        if words_ is None or self.model is None or register not in self.model:
            return None
        decoded = decode(self.model.register(register), *words_, high_word_first=True)
        return decoded.value

    def _store(self, register: int, words_: tuple[int, int]) -> None:
        now = time.monotonic()
        if register == profile.PRIO:
            prio = words_[0] & 0xFF
            previous = self._samples.get(register)
            if prio != profile.PRIO_HOT_WATER:
                self._charge_started = None
            elif previous is None or previous.words[0] & 0xFF != profile.PRIO_HOT_WATER:
                self._charge_started = now
        self._samples[register] = Sample(words_, now)
        tick, self._tick = self._tick, asyncio.Event()
        tick.set()

    def _pump(self) -> tuple[ModelMap, profile.Layout]:
        if self.model is None or self.layout is None:
            raise NotIdentified("the pump isn't identified yet")
        return self.model, self.layout

    async def _poll(self, send: Send) -> None:
        model, layout = self._pump()
        rule_inputs = (
            profile.PRIO,
            profile.COMPRESSOR,
            profile.SUPPLY_PUMP_SPEED,
            profile.BRINE_PUMP_SPEED,
        )
        wanted = sorted(
            {p.register for p in layout.points.values()} | {r for r in rule_inputs if r in model}
        )
        while True:
            polled = [r for r in wanted if r not in self.pushed and r not in self.absent]
            if not polled:
                await asyncio.sleep(1.0)
            for register in polled:
                if register not in self.pushed:
                    await self._read(register)
                    await self._drop_if_absent(register, send)

    async def _drop_if_absent(self, register: int, send: Send) -> None:
        """A 32-bit point whose register gives no value (a heat meter this pump doesn't
        keep) is removed from the description and not read again. The pump is described
        before this is known, so start-up never waits on these reads; a point that later
        starts giving values is found at the next start."""
        model, layout = self._pump()
        definition = model.register(register)
        sample = self._samples.get(register)
        if sample is None or definition.size is None or definition.size.bits != 32:
            return
        if self.high_word_first is None:
            return
        decoded = decode(definition, *sample.words, high_word_first=self.high_word_first)
        if decoded.status is not Status.NO_VALUE:
            return
        gone = [path for path, d in layout.points.items() if d.register == register]
        for path in gone:
            del layout.points[path]
            log.info("%s (register %d) gives no value: left out", path, register)
        self.absent.add(register)
        if gone:
            removed = tuple(f"{profile.UNIT}/{path}" for path in gone)
            await send(Described(complete=False, removed=removed))

    async def _read_points(self, request: Read) -> list[Envelope]:
        if request.after is not None:
            after = time.monotonic() - (_now() - request.after).total_seconds()
            for path in request.points:
                definition = self._definition(path)
                if definition is None:
                    continue
                if definition.register in self.pushed:
                    await self._wait_for(definition.register, after)
                else:
                    await self._read(definition.register, after=after)
        return [self.envelope(path) for path in request.points]

    async def _wait_for(self, register: int, after: float) -> None:
        with contextlib.suppress(TimeoutError):
            async with asyncio.timeout(self._read_timeout):
                while (s := self._samples.get(register)) is None or s.t <= after:
                    await self._tick.wait()

    async def _subscription(self, request: Subscribe, send: Send) -> None:
        sent: dict[str, tuple[object, ...]] = {}
        while True:
            values = []
            for path in request.points:
                envelope = self.envelope(path)
                state = (envelope.value, envelope.quality, envelope.why)
                if not request.on_change or sent.get(path) != state:
                    values.append(envelope)
                    sent[path] = state
            if values:
                await send(Update(id=request.id, values=tuple(values)))
            await self._tick.wait()
            if request.min_interval_s:
                await asyncio.sleep(request.min_interval_s)

    # --- values ----------------------------------------------------------------------------

    def _definition(self, path: str) -> profile.PointDef | None:
        prefix = f"{profile.UNIT}/"
        if self.layout is None or not path.startswith(prefix):
            return None
        return self.layout.points.get(path[len(prefix) :])

    def envelope(self, path: str) -> Envelope:
        definition = self._definition(path)
        now = _now()
        if definition is None or self.model is None:
            return _missing(path, now, "no such point")
        register = self.model.register(definition.register)
        sample = self._samples.get(definition.register)
        unit = self._unit(definition)
        if sample is None:
            return _missing(path, now, "not read yet")
        observed = now - timedelta(seconds=time.monotonic() - sample.t)
        if register.size is None:
            return _missing(path, now, "the register's size isn't known")
        if register.size.bits == 32 and self.high_word_first is None:
            return _missing(path, now, "the word order (48852) couldn't be read")
        decoded = decode(register, *sample.words, high_word_first=bool(self.high_word_first))
        quality: Quality = "good"
        why: str | None = None
        value: bool | int | float | str | None = decoded.value
        if decoded.status is Status.NOT_CONNECTED:
            quality, why, value = "not_connected", "the pump reports no sensor", None
        elif decoded.status is Status.NO_VALUE:
            quality, why, value = "unknown", "the pump gives no value", None
        elif decoded.status is Status.OUT_OF_RANGE:
            quality, why = "out_of_range", f"outside {register.min}..{register.max}"
        elif is_switch(register) and definition.enum is None:
            value = bool(decoded.raw)
        elif (enum := definition.enum or value_texts(register)) is not None:
            name = enum.get(int(decoded.raw or 0))
            if name is None:
                quality, value = "unknown", None
                why = definition.unknown_why or f"unknown value {decoded.raw}"
            else:
                value = name
        if quality == "good":
            snapshot = self._snapshot()
            for rule in definition.rules:
                verdict = rule(snapshot)
                if verdict is not None:
                    quality, why = verdict
                    break
        freshness = PUSHED_FRESHNESS_S if definition.register in self.pushed else POLLED_FRESHNESS_S
        if quality == "good" and time.monotonic() - sample.t > freshness:
            quality, why = "stale", f"last read {int(time.monotonic() - sample.t)} s ago"
        return Envelope(
            point=path,
            value=value,
            unit=unit if value is not None else None,
            raw=decoded.raw,
            t_observed=observed,
            t_received=observed,
            quality=quality,
            source=definition.source,
            resolution=1 / register.factor if definition.enum is None else None,
            why=why,
        )

    def _snapshot(self) -> profile.Snapshot:
        model, _ = self._pump()
        values: dict[int, float | int | None] = {}
        for register, sample in self._samples.items():
            if register in model:
                definition = model.register(register)
                if definition.size is not None and definition.size.bits <= 16:
                    values[register] = decode(definition, *sample.words, high_word_first=True).value
        return profile.Snapshot(values, self._charge_started, time.monotonic())

    def _unit(self, definition: profile.PointDef) -> str | None:
        if definition.enum is not None or self.model is None:
            return None
        name = definition.path.rsplit("/", 1)[-1]
        node = definition.path.rsplit("/", 1)[0] if "/" in definition.path else ""
        kind = self.layout.nodes.get(node, "unit") if self.layout else "unit"
        standard = None if name.startswith("x.") else standard_point(kind, name)
        if standard is not None and standard.unit is not None:
            return standard.unit
        return profile.unit(self.model.register(definition.register).unit)

    @property
    def problem(self) -> str | None:
        """Why the pump couldn't be identified, once that is known."""
        f = self._identified
        if not f.done() or f.cancelled() or f.exception() is None:
            return None
        return str(f.exception())

    # --- describing ------------------------------------------------------------------------

    def describe(self, id: int | None = None) -> Described:
        model, layout = self._pump()
        unit = profile.UNIT
        promises = self._transport.promises if self._transport else None
        nodes = [
            Node(
                path=unit,
                kind="unit",
                presence=Presence(how="configured"),
                label=self._product.result() if self._product.done() else model.name,
                identity=Identity(
                    vendor="Nibe",
                    model=model.name,
                    firmware=self.firmware,
                    map=f"nibe-bus-{model.name}",
                ),
                transport=Promises(
                    fate="exact"
                    if promises is not None and promises.fate is FateKind.EXACT
                    else "best_effort",
                    ack=Ack(means="accepted", detail="0x6C: 1 accepted, 0 refused"),
                    sees_other_writers=Knowledge(
                        value="yes" if promises and promises.sees_other_writers else "no",
                        known="reported",
                    ),
                ),
            )
        ]
        for path, kind in layout.nodes.items():
            nodes.append(
                Node(path=f"{unit}/{path}", kind=kind, presence=self._presence(path, kind))
            )
        points = [self._point(d) for d in layout.points.values()]
        levers = profile.levers(model, layout.points)
        return Described(id=id, nodes=tuple(nodes), points=tuple(points), levers=tuple(levers))

    def _presence(self, path: str, kind: str) -> Presence:
        if kind != "climate_system":
            return Presence(how="assumed")
        number = int(path.removeprefix("cs"))
        if number == 1:
            return Presence(how="detected", rule="climate system 1 is always there")
        system = profile.SYSTEMS[number - 1]
        return Presence(
            how="detected",
            rule=f"{system.accessory} = 1 and supply sensor {system.supply} connected",
        )

    def _point(self, definition: profile.PointDef) -> Point:
        model, _ = self._pump()
        register = model.register(definition.register)
        unit = self._unit(definition)
        pushed = definition.register in self.pushed
        range_ = Knowledge[Range]()
        if register.min is not None and register.max is not None and definition.enum is None:
            range_ = Knowledge(
                value=Range(min=register.min / register.factor, max=register.max / register.factor),
                known="documented",
                basis="Nibe register database",
            )
        enum = Knowledge[dict[str, int | str]]()
        texts = definition.enum or (None if is_switch(register) else value_texts(register))
        if texts is not None:
            enum = Knowledge(
                value={name: raw for raw, name in texts.items()},
                known="documented",
                basis="Nibe register database",
            )
        whole = texts is None and not is_switch(register)
        wraps_at = None
        if definition.path.startswith(COUNTERS) and register.size is not None:
            wraps_at = (1 << register.size.bits) / register.factor
        return Point(
            path=f"{profile.UNIT}/{definition.path}",
            label=register.title if definition.path.rpartition("/")[2].startswith("x.") else None,
            description=(register.info or "").strip() or None,
            category=_category(definition, register),
            unit=unit,
            wraps_at=wraps_at,
            resolution=Knowledge(value=1 / register.factor, known="documented")
            if whole
            else Knowledge[float](),
            range=range_,
            enum=enum,
            delivery=Delivery(how="pushed", interval_s=0.5)
            if pushed
            else Delivery(how="polled", cost_s=1.0),
            freshness_s=Knowledge(
                value=PUSHED_FRESHNESS_S if pushed else POLLED_FRESHNESS_S,
                known="documented",
                basis="this plugin's reading schedule",
            ),
            validity=definition.validity,
        )

    def _health(self) -> Health:
        if self._transport is None:
            return Health(t=_now(), unit=profile.UNIT, state="down")
        health = self._transport.health()
        last = None
        if health.last_traffic is not None:
            last = _now() - timedelta(seconds=time.monotonic() - health.last_traffic)
        return Health(
            t=_now(),
            unit=profile.UNIT,
            state="up" if health.up else "down",
            last_traffic=last,
            counters=dict(health.detail),
        )


def _category(
    definition: profile.PointDef, register: Register
) -> Literal["config", "diagnostic"] | None:
    """A standard point is worth seeing every day; a pump setting read back is `config`;
    any other register of the pump's own is `diagnostic`, and so is the word order."""
    if definition.register == profile.WORD_SWAP:
        return "diagnostic"
    if not definition.path.rpartition("/")[2].startswith("x."):
        return None
    return "config" if register.writable else "diagnostic"


_TEXT = re.compile(r"(-?\d+)\s*=\s*([^,=]+?)\s*(?=,|\s+-?\d+\s*=|$)")


def value_texts(register: Register) -> dict[int, str] | None:
    """What a register's values mean, from its notes in the register database: "0=Auto
    1=Manual 2=Add. heat only", "0=Off, 1=3h, 2=6h". None where the notes don't say."""
    if register.factor != 1:
        return None
    out: dict[int, str] = {}
    for raw, text in _TEXT.findall(register.info or ""):
        out.setdefault(int(raw), text.strip().rstrip("."))
    return out if len(out) >= 2 else None


def is_switch(register: Register) -> bool:
    """A setting that is only off or on: 0 and 1, with no other meanings given (or just
    "0=Off 1=On")."""
    if register.factor != 1 or (register.min, register.max) != (0, 1):
        return False
    texts = value_texts(register)
    return texts is None or {k: v.lower() for k, v in texts.items()} == {0: "off", 1: "on"}


def _now() -> datetime:
    return datetime.now(UTC)


def _missing(path: str, now: datetime, why: str) -> Envelope:
    return Envelope(
        point=path,
        value=None,
        unit=None,
        t_observed=None,
        t_received=now,
        quality="unknown",
        source="measured",
        why=why,
    )


def create(context: PluginContext) -> NibePlugin:
    """The entry point: a plugin instance from its settings."""
    return NibePlugin(NibeGateway.model_validate(dict(context.settings)), secrets=context.secrets)
