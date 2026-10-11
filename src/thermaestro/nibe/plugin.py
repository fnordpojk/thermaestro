"""The Nibe plugin: the bus family (F-series, VVM, SMO, MHB) through a gateway, and the
S-series over its own Modbus TCP.

On the bus it connects to the pump's gateway, identifies the pump (its model from the
product information it sends every 15 s, its firmware from 43001/44331, the word order of
32-bit values from 48852), finds the climate systems and pools it has, and then keeps
every point fresh: registers the pump pushes (its LOG.SET) as they come, the rest polled
one at a time.

An S-series pump names neither its model nor its firmware in a documented register, so
its model is set by the user. Modbus's own device identification is asked for and shown,
not relied on. Every point is polled, one value per request. Its levers are described as
unavailable: none has been tried on a real S-series pump.

It writes only when the core asks: with an `act` on a lever, never a register outside
that lever's own; or, on the bus, with a `write` of one of the pump's own settings, which
the core sends only for a person. Any register of the model can be read as
`x.nibe.<register>`, described or not. A hold's release (what the hot-water block puts back)
is kept across restarts. Where the route shows other clients' writes, each is reported as a
`foreign_write`.
"""

import asyncio
import contextlib
import logging
import re
from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Literal

from thermaestro_gateway import nibe
from thermaestro_gateway.protocol import Origin, Stage

from .. import clock, durations
from ..cap import Message, Send
from ..cap.messages import (
    Act,
    Describe,
    Described,
    DeviceEvent,
    Error,
    Fate,
    FateStage,
    ForeignWrite,
    Health,
    Read,
    Subscribe,
    Update,
    Values,
    Write,
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
from ..core.plugins import PluginContext, PluginStore
from ..store import NibeGateway, SecretStore
from . import profile, sprofile
from .maps import ModelMap, Register, RegisterMap, Status, decode, load, words
from .maps.codec import EncodeError, encode
from .transport import GatewayConfig, ModbusConfig, connect
from .transport.base import (
    FateKind,
    Observed,
    ReadFailed,
    RegisterRefused,
    Transport,
    WriteOutcome,
    WriteResult,
    stage_after_reply,
)
from .transport.nibegw import PlainSettings

log = logging.getLogger(__name__)

PRODUCT_INFO = 0x6D
LOG_SET = 0x68
READ_TIMEOUT_S = 5.0
PUSHED_FRESHNESS_S = 30.0
POLLED_FRESHNESS_S = 900.0
METER_SAVE_S = 60.0
"""How often the heat meters' counts are kept, at most, for a restart."""
COUNTERS = ("heat.produced", "elec.used")
"""Points that only count up, and start over from 0 at their register's size."""
IDENTIFICATION = "x.nibe.identification"
REGISTER = re.compile(r"^x\.nibe\.(\d+)$")
"""Any register of the model, as a point directly under the unit."""
"""What an S-series pump answers to Modbus's device identification, as text."""
WRITE_TIMEOUT_S = 30.0
"""The longest a write waits for its turn on the bus and the pump's answer."""
FATES: dict[WriteResult, FateStage] = {
    WriteResult.ACCEPTED: "device_accepted",
    WriteResult.REFUSED: "device_refused",
    WriteResult.NOT_TAKEN: "dropped",
    WriteResult.UNKNOWN: "unknown",
}


class Refused(Exception):
    """A request the plugin won't send, and why."""


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
        state: PluginStore | None = None,
        poll_round_s: float | None = None,
    ) -> None:
        self.gateway = gateway
        self.family = sprofile.S_SERIES if gateway.protocol == "modbus-tcp" else profile.BUS
        if self.family.name == "bus":
            self.features = ("subscribe", "write")
        self._poll_round = self.family.poll_round_s if poll_round_s is None else poll_round_s
        self._state = state
        self._secrets = secrets
        self.maps = maps or load(self.family.name)
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
        self.identification: dict[str, str] | None = None
        """An S-series pump's answer to Modbus device identification, where it gave one."""
        self.layout: profile.Layout | None = None
        self._samples: dict[int, Sample] = {}
        self._decoded: dict[int, float | int | None] | None = None
        """The samples decoded, until the next is stored."""
        self.pushed: set[int] = set()
        self.absent: set[int] = set()
        """Registers that gave no value, whose points were removed."""
        self._refused: set[int] = set()
        """Registers the pump said it hasn't got."""
        self._tick = asyncio.Event()
        self._read_lock = asyncio.Lock()
        self._charge_started: float | None = None
        self._returned: float | None = None
        """When the demand last left hot water or pool."""
        self._meter_idle: dict[int, float] = {}
        """Per heat meter: seconds of production for its purpose since it last changed."""
        self._last_store: float | None = None
        self._restored: dict[int, tuple[tuple[int, int], float]] = {}
        """Per heat meter, as kept before a restart: its words then, and its count."""
        self._saved_at = 0.0
        self._last_saved: dict[str, Any] = {}
        self._saving: asyncio.Task[None] | None = None
        self._save_lock = asyncio.Lock()
        self.holds: dict[str, dict[str, Any]] = {}
        """Per hold lever engaged: the register written and the value to put back (and for
        the hot-water block, the mode it was engaged in). Kept across restarts."""
        self._act_lock = asyncio.Lock()
        self._mine: Counter[bytes] = Counter()
        """The write requests this plugin has on their way, to tell other clients' apart."""
        self._send_event: Send | None = None
        self._described_mode: int | None = None
        """The hot-water mode the block's description was last given for."""
        self._block_described = False
        self._tasks: set[asyncio.Future[None]] = set()
        self._extracted_kwh = 0.0
        """The estimated heat taken from the ground, kWh, while Thermaestro runs."""
        self._integrated_at: float | None = None
        self._brine_warned = False

    # --- the plugin interface --------------------------------------------------------------

    async def events(self, send: Send) -> None:
        if self._transport is not None or self._identified.done():
            raise RuntimeError("a Nibe plugin instance serves one connection")
        self._send_event = send
        await self._restore()
        try:
            self._transport = await self._connect_transport()
        except NotIdentified as e:
            await send(Health(t=_now(), unit=profile.UNIT, state="down", needs_user_action=None))
            self._identified.set_exception(e)
            raise
        except Exception as e:
            await send(Health(t=_now(), unit=profile.UNIT, state="down", needs_user_action=None))
            reached = "the pump" if self.family.name == "s-series" else "the gateway"
            self._identified.set_exception(NotIdentified(f"can't reach {reached}: {e}"))
            raise
        stop = self._transport.observe(self._observed)
        poller: asyncio.Task[None] | None = None
        try:
            await self._identify()
            self._identified.set_result(None)
            poller = asyncio.create_task(self._poll(send))
            while True:
                await send(self._health())
                if (event := self._brine_warning()) is not None:
                    await send(event)
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
        offered = isinstance(request, Describe | Read | Subscribe | Act) or (
            isinstance(request, Write) and "write" in self.features
        )
        if not offered:
            await send(
                Error(id=getattr(request, "id", None), code="unsupported", detail="not offered")
            )
            return
        await asyncio.shield(self._identified)
        if isinstance(request, Act):
            async with self._act_lock:  # one change at a time: the bus carries one anyway
                await self._act(request, send)
        elif isinstance(request, Write):
            async with self._act_lock:
                await self._write_setting(request, send)
        elif isinstance(request, Describe):
            await send(self.describe(request.id))
        elif isinstance(request, Read):
            await send(Values(id=request.id, values=tuple(await self._read_points(request))))
        elif isinstance(request, Subscribe):
            await self._subscription(request, send)

    # --- connecting and identifying ---------------------------------------------------------

    async def _connect_transport(self) -> Transport:
        if self.family.name == "s-series":
            # The model is the user's: a Modbus TCP pump doesn't name it.
            try:
                model = self.maps.model(self.gateway.model or "")
            except KeyError as e:
                raise NotIdentified(f"{e.args[0]}; choose one of the S-series models") from None
            wide = frozenset(
                r for r in model if (size := model.register(r).size) is not None and size.bits == 32
            )
            modbus = ModbusConfig(self.gateway.host, self.gateway.modbus_port, wide=wide)
            return await self._connect(modbus, **self._transport_settings)
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
        if self.family.name == "s-series":
            await self._identify_modbus()
            return
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
        for system in self.family.detectable(self.model):
            if system.accessory is None or await self._read_value(system.accessory) != 1:
                continue
            words_ = await self._read(system.supply)
            if words_ is not None:
                supply = decode(self.model.register(system.supply), *words_, high_word_first=True)
                if supply.status is not Status.NOT_CONNECTED:
                    systems.append(system.number)
        pools = []
        for pool in self.family.detectable_pools(self.model):
            if await self._read_value(pool.accessory) != 1:
                continue
            words_ = await self._read(pool.sensor)
            if words_ is not None:
                sensor = decode(self.model.register(pool.sensor), *words_, high_word_first=True)
                if sensor.status is not Status.NOT_CONNECTED:
                    pools.append(pool.number)
        if (mode := self.family.hot_water_mode) is not None and mode in self.model:
            await self._read(mode)  # the hot-water block's description names it
        self.layout = self.family.layout(self.model, systems, pools)
        log.info(
            "pump %s, firmware %s, climate systems %s, pools %s",
            name,
            self.firmware,
            systems,
            pools or "none",
        )

    async def _identify_modbus(self) -> None:
        """The user's model; a 32-bit value's words come back put in order by the transport.
        The pump answers its Modbus TCP, so it's there; what it says of itself is kept."""
        self.model = self.maps.model(self.gateway.model or "")
        self.high_word_first = True
        identify = getattr(self._transport, "identify", None)
        if identify is not None:
            self.identification = await identify()
        self.layout = self.family.layout(self.model, [1])
        log.info(
            "pump %s (set by the user), identification %s", self.model.name, self.identification
        )

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
        elif telegram.is_token and telegram.command == nibe.WRITE_TOKEN:
            self._seen_write(observed)

    def _seen_write(self, observed: Observed) -> None:
        """A write the pump took: another client's is reported, where the route shows it."""
        reply = observed.reply
        if len(reply) != 10 or reply[1] != nibe.WRITE_TOKEN:
            return
        if stage_after_reply(observed.trailer) is Stage.PUMP_NAK:
            return  # not taken
        if observed.origin is None:
            if self._mine[reply]:
                return
        elif observed.origin not in (Origin.PLAIN_CLIENT, Origin.OTHER_CLIENT):
            return
        register = int.from_bytes(reply[3:5], "little")
        raw = int.from_bytes(reply[5:9], "little")
        value: float | int | None = raw
        if self.model is not None and register in self.model:
            definition = self.model.register(register)
            if definition.size is not None and definition.size.bits <= 16:
                value = decode(definition, raw & 0xFFFF, high_word_first=True).value
        log.info("another client wrote %d = %s", register, value)
        if self._send_event is not None:
            event = ForeignWrite(
                t=_now(), unit=profile.UNIT, datapoint=f"x.nibe.{register}", value=value
            )
            self._spawn(self._send_event(event))

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
                if isinstance(e, RegisterRefused):
                    self._refused.add(register)
                return None
        first, second = words(reading.data)
        self._store(register, (first, second))
        following = register + 1
        if (
            self.family.answers_carry_next
            and self.model is not None
            and following in self.model
            and following not in self.pushed
        ):
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
        now = clock.monotonic()
        self._watch_meters(register, words_, now)
        if register == self.family.prio:
            prio = words_[0] & 0xFF
            previous = self._samples.get(register)
            before = None if previous is None else previous.words[0] & 0xFF
            if prio != self.family.prio_hot_water:
                self._charge_started = None
            elif before != self.family.prio_hot_water:
                self._charge_started = now
            elsewhere = self.family.prio_elsewhere
            if prio in elsewhere:
                self._returned = None
            elif before in elsewhere:
                self._returned = now
        self._samples[register] = Sample(words_, now)
        self._decoded = None
        if register in (self.family.brine_in, self.family.brine_out, self.family.brine_pump_speed):
            self._integrate(now)
        if register == self.family.hot_water_mode:
            self._mode_seen(words_[0] & 0xFF)
        tick, self._tick = self._tick, asyncio.Event()
        tick.set()

    def _mode_seen(self, mode: int) -> None:
        """The hot-water block lowers the current mode's start: when the mode changes, the
        block is described anew, so the core can move it."""
        if not self._block_described or mode == self._described_mode:
            return
        self._described_mode = mode
        block = self._specs().get(f"{profile.UNIT}/dhw/block")
        if block is not None and self._send_event is not None:
            log.info("hot-water mode now %s: the block is described anew", mode)
            self._spawn(self._send_event(Described(complete=False, levers=(block.lever,))))

    def _watch_meters(self, register: int, words_: tuple[int, int], now: float) -> None:
        """Count the production each heat meter should have counted since it last moved:
        the time the compressor ran while the demand was the meter's purpose."""
        if self._last_store is not None:
            elapsed = min(now - self._last_store, 60.0)  # a gap in reading isn't production
            prio = self._samples.get(self.family.prio)
            compressor = self._samples.get(self.family.compressor)
            running = (
                compressor is not None
                and compressor.words[0] & 0xFF == self.family.compressor_running
            )
            demand = self.family.demand.get(prio.words[0] & 0xFF) if prio is not None else None
            if running and demand is not None:
                for meter, purpose in self.family.meters.items():
                    if purpose == demand and meter in self._samples:
                        self._meter_idle[meter] = self._meter_idle.get(meter, 0.0) + elapsed
        self._last_store = now
        if register in self.family.meters:
            previous = self._samples.get(register)
            if previous is None:
                # The count kept before a restart holds if the meter still reads the same.
                kept = self._restored.pop(register, None)
                if kept is not None and kept[0] == words_:
                    self._meter_idle[register] = kept[1]
            elif previous.words != words_:
                self._meter_idle[register] = 0.0
        if self._state is not None and now - self._saved_at >= METER_SAVE_S:
            self._save_meters(now)

    async def _restore(self) -> None:
        if self._state is None:
            return
        data = await self._state.load()
        try:
            self._restored = {
                int(register): ((int(m["words"][0]), int(m["words"][1])), float(m["idle"]))
                for register, m in data.get("meters", {}).items()
            }
            self._extracted_kwh = float(data.get("brine_kwh", 0.0))
        except (KeyError, TypeError, ValueError, AttributeError) as e:
            log.warning("what was kept couldn't be read, so the counts start over: %s", e)
        holds = data.get("holds", {})
        for path, hold in holds.items() if isinstance(holds, dict) else ():
            if (
                isinstance(hold, dict)
                and isinstance(hold.get("register"), int)
                and isinstance(hold.get("value"), int | float)
            ):
                self.holds[str(path)] = dict(hold)
            else:
                log.error("the hold kept for %s can't be read: %r", path, hold)

    def _save_meters(self, now: float) -> None:
        """Keep the heat meters' counts and the heat taken from the ground for a restart,
        at most once a minute."""
        if self._state is None or (self._saving is not None and not self._saving.done()):
            return
        self._saved_at = now
        self._saving = asyncio.get_running_loop().create_task(self._save())

    async def _save(self) -> None:
        """Keep what outlives a restart. What is saved is put together under the lock, so
        a later save never carries older holds than an earlier one."""
        if self._state is None:
            return
        async with self._save_lock:
            meters = {
                str(register): {
                    "words": list(self._samples[register].words),
                    "idle": round(idle, 1),
                }
                for register, idle in self._meter_idle.items()
                if register in self._samples
            }
            data: dict[str, Any] = {
                "meters": meters,
                "brine_kwh": round(self._extracted_kwh, 3),
                "holds": {path: dict(hold) for path, hold in self.holds.items()},
            }
            if data == self._last_saved:
                return  # nothing new: no write, which on a Pi's SD card counts
            await self._state.save(data)
            self._last_saved = data

    def _brine_warning(self) -> DeviceEvent | None:
        """A warning when brine out comes within `BRINE_WARNING_K` of the pump's own low
        brine-out alarm limit while the compressor runs, before the pump's alarm; it ends
        a little above that, so it doesn't flicker. None when nothing changed."""
        if self.layout is None:
            return None
        values = self._snapshot().values
        brine_out = values.get(self.family.brine_out)
        limit = values.get(self.family.brine_out_limit)
        compressor = values.get(self.family.compressor)
        if brine_out is None or limit is None:
            return None
        running = compressor is not None and int(compressor) == self.family.compressor_running
        if not self._brine_warned and running and brine_out <= limit + profile.BRINE_WARNING_K:
            self._brine_warned = True
        elif self._brine_warned and brine_out > limit + profile.BRINE_WARNING_CLEAR_K:
            self._brine_warned = False
        else:
            return None
        return DeviceEvent(
            t=_now(),
            unit=profile.UNIT,
            code="brine.out.low",
            text=(
                f"Brine out is {brine_out:.1f} °C, within {profile.BRINE_WARNING_K:g} °C of the"
                f" pump's own low brine-out alarm limit ({limit:.1f} °C)"
            ),
            active=self._brine_warned,
        )

    def _integrate(self, now: float) -> None:
        """Add up the estimated heat taken from the ground, between readings."""
        last, self._integrated_at = self._integrated_at, now
        if self.gateway.brine_flow is None or last is None or self.layout is None:
            return
        brine = self._brine()
        if isinstance(brine, str) or brine[2] == 0:
            return
        hours = min(now - last, 60.0) / 3600  # a gap in reading isn't counted
        self._extracted_kwh += max(0.0, self._extraction_kw(*brine)) * hours

    def _pump(self) -> tuple[ModelMap, profile.Layout]:
        if self.model is None or self.layout is None:
            raise NotIdentified("the pump isn't identified yet")
        return self.model, self.layout

    async def _poll(self, send: Send) -> None:
        model, layout = self._pump()
        watched = {r for r in self.family.watched if r in model}
        wanted = sorted({p.register for p in layout.points.values()} | watched)
        while True:
            started = clock.monotonic()
            polled = [r for r in wanted if r not in self.pushed and r not in self.absent]
            if not polled:
                await asyncio.sleep(1.0)
            for register in polled:
                if register not in self.pushed:
                    await self._read(register)
                    await self._drop_if_absent(register, send)
            await asyncio.sleep(max(0.0, self._poll_round - (clock.monotonic() - started)))

    async def _drop_if_absent(self, register: int, send: Send) -> None:
        """A point whose register the pump refuses (an S-series pump without the accessory)
        or that gives no value (a 32-bit heat meter this pump doesn't keep) is removed from
        the description and not read again, with what is worked out from it. The pump is
        described before this is known, so start-up never waits on these reads; a point
        that later starts giving values is found at the next start."""
        model, layout = self._pump()
        if register in self._refused:
            why = "the pump hasn't got it"
        else:
            definition = model.register(register)
            sample = self._samples.get(register)
            if sample is None or definition.size is None or definition.size.bits != 32:
                return
            if self.high_word_first is None:
                return
            decoded = decode(definition, *sample.words, high_word_first=self.high_word_first)
            if decoded.status is not Status.NO_VALUE:
                return
            why = "gives no value"
        gone = [path for path, d in layout.points.items() if d.register == register]
        for path in gone:
            del layout.points[path]
            log.info("%s (register %d) %s: left out", path, register, why)
        for path in [path for path, d in layout.derived.items() if register in d.inputs]:
            del layout.derived[path]
            gone.append(path)
        self.absent.add(register)
        if gone:
            removed = tuple(f"{profile.UNIT}/{path}" for path in gone)
            await send(Described(complete=False, removed=removed))

    async def _read_points(self, request: Read) -> list[Envelope]:
        if request.after is not None:
            after = clock.monotonic() - (_now() - request.after).total_seconds()
            for path in request.points:
                definition = self._definition(path)
                if definition is None:
                    continue
                if definition.register in self.pushed:
                    await self._wait_for(definition.register, after)
                else:
                    await self._read(definition.register, after=after)
        found = [self.envelope(path) for path in request.points]
        if request.after is None:
            return found
        # A read that failed leaves the older sample: that is no answer to this read.
        return [
            _missing(e.point, _now(), "no value read since it was asked")
            if e.t_observed is not None and e.t_observed < request.after
            else e
            for e in found
        ]

    async def _wait_for(self, register: int, after: float) -> None:
        with contextlib.suppress(TimeoutError):
            async with asyncio.timeout(self._read_timeout):
                while (s := self._samples.get(register)) is None or s.t <= after:
                    await self._tick.wait()

    async def _subscription(self, request: Subscribe, send: Send) -> None:
        sent: dict[str, tuple[object, ...]] = {}
        made: dict[str, tuple[object, ...]] = {}
        """Per point: what its last envelope was made from, where only its own sample and
        its age decide it; such a point isn't made again until either changes."""
        while True:
            values = []
            for path in request.points:
                basis = self._basis(path) if request.on_change else None
                if basis is not None:
                    if made.get(path) == basis:
                        continue
                    made[path] = basis
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

    def _basis(self, path: str) -> tuple[object, ...] | None:
        """What a point's envelope depends on, where that is only its own sample and
        whether it is stale; None where rules or other registers come into it."""
        definition = self._definition(path)
        if definition is None or definition.rules or self._derived(path) is not None:
            return None
        sample = self._samples.get(definition.register)
        if sample is None:
            return None
        freshness = PUSHED_FRESHNESS_S if definition.register in self.pushed else POLLED_FRESHNESS_S
        return sample, clock.monotonic() - sample.t > freshness, self.high_word_first

    # --- values ----------------------------------------------------------------------------

    def _definition(self, path: str) -> profile.PointDef | None:
        """A described point, or any register of the model directly under the unit."""
        prefix = f"{profile.UNIT}/"
        if self.layout is None or not path.startswith(prefix):
            return None
        below = path[len(prefix) :]
        found = self.layout.points.get(below)
        if found is not None:
            return found
        match = REGISTER.match(below)
        if match is None or self.model is None or int(match[1]) not in self.model:
            return None
        return profile.PointDef(below, int(match[1]))

    def _derived(self, path: str) -> profile.Derived | None:
        """A point worked out from the pump's values: the brine's delta-T always; the
        heat taken from the ground only with a brine flow entered."""
        prefix = f"{profile.UNIT}/"
        if self.layout is None or not path.startswith(prefix):
            return None
        found = self.layout.derived.get(path[len(prefix) :])
        if found is None or (found.source == "estimated" and self.gateway.brine_flow is None):
            return None
        return found

    def _brine(self) -> tuple[float, float, float] | str:
        """Brine in, brine out and the brine pump's speed, or why there are none."""
        values = self._snapshot().values
        found = [values.get(r) for r in (self.family.brine_in, self.family.brine_out)]
        speed = values.get(self.family.brine_pump_speed)
        if any(r not in self._samples for r in self.family.brine_derived[0].inputs):
            return "not read yet"
        if found[0] is None or found[1] is None or speed is None:
            return "a brine sensor gives no value"
        return float(found[0]), float(found[1]), float(speed)

    def _extraction_kw(self, brine_in: float, brine_out: float, speed: float) -> float:
        """The heat taken from the ground now: the entered flow scaled by the brine pump's
        speed, times what a liter of the brine carries per kelvin, times the delta-T."""
        flow = self.gateway.brine_flow or 0.0
        liters_per_s = flow * speed / self.gateway.brine_flow_at / 60
        return liters_per_s * profile.BRINE_HEAT[self.gateway.brine_mix] * (brine_in - brine_out)

    def _setting_envelope(self, path: str, d: profile.Derived) -> Envelope:
        """A setting under its standard name: for one that follows the hot-water mode, the
        current mode's; while a hold of this plugin's lowers it, what its release puts
        back."""
        now = _now()
        values = self._snapshot().values
        register = d.inputs[0]
        if d.by_mode is not None:
            mode = values.get(register)
            if register not in self._samples or mode is None:
                return _missing(path, now, "the hot-water mode isn't read yet")
            found = d.by_mode.get(int(mode))
            if found is None:
                title = profile.MODE_TITLES.get(int(mode), f"mode {mode:g}")
                return _missing(path, now, f"{title} has no setting of its own")
            register = found
        value = values.get(register)
        if register not in self._samples or value is None:
            return _missing(path, now, "not read yet")
        for hold in self.holds.values():
            if hold.get("register") == register:
                value = hold["value"]
        t = min(self._samples[r].t for r in {d.inputs[0], register})
        observed = now - timedelta(seconds=clock.monotonic() - t)
        quality: Quality = "good"
        why = None
        if clock.monotonic() - t > POLLED_FRESHNESS_S:
            quality, why = "stale", f"last read {durations.text(int(clock.monotonic() - t))} ago"
        return Envelope(
            point=path,
            value=round(float(value), 1),
            unit=d.unit,
            t_observed=observed,
            t_received=observed,
            quality=quality,
            source=d.source,
            resolution=d.resolution,
            why=why,
        )

    def _derived_envelope(self, path: str, d: profile.Derived) -> Envelope:
        if d.setting:
            return self._setting_envelope(path, d)
        now = _now()
        brine = self._brine()
        if isinstance(brine, str):
            return _missing(path, now, brine)
        brine_in, brine_out, speed = brine
        t = max(self._samples[r].t for r in d.inputs)
        observed = now - timedelta(seconds=clock.monotonic() - t)
        quality: Quality = "good"
        why: str | None = None
        value: float
        if d.source == "estimated":
            why = (
                f"estimated from {self.gateway.brine_flow:g} L/min at "
                f"{self.gateway.brine_flow_at} % and the brine's delta-T"
            )
        if d.path.endswith("heat.extracted"):
            value = round(self._extracted_kwh, 1)
        elif d.path.endswith("power"):
            # A brine pump standing still takes no heat from the ground.
            value = 0.0 if speed == 0 else round(self._extraction_kw(brine_in, brine_out, speed), 2)
        else:
            value = round(brine_in - brine_out, 1)
            if speed == 0:
                # No flow: the two sensors measure standing brine, as their own values say.
                quality, why = "no_flow", profile.BRINE_STOPPED
            elif (verdict := self.family.compressor_changing(self._snapshot())) is not None:
                quality, why = verdict
        return Envelope(
            point=path,
            value=value,
            unit=d.unit,
            t_observed=observed,
            t_received=observed,
            quality=quality,
            source=d.source,
            resolution=d.resolution,
            why=why,
        )

    def envelope(self, path: str) -> Envelope:
        derived = self._derived(path)
        if derived is not None:
            return self._derived_envelope(path, derived)
        if path == f"{profile.UNIT}/{IDENTIFICATION}" and self.family.name == "s-series":
            return self._identification_envelope(path)
        definition = self._definition(path)
        now = _now()
        if definition is None or self.model is None:
            return _missing(path, now, "no such point")
        register = self.model.register(definition.register)
        sample = self._samples.get(definition.register)
        unit = self._unit(definition)
        if sample is None:
            return _missing(path, now, "not read yet")
        observed = now - timedelta(seconds=clock.monotonic() - sample.t)
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
        if quality == "good" and definition.rules:
            snapshot = self._snapshot()
            for rule in definition.rules:
                verdict = rule(snapshot)
                if verdict is not None:
                    quality, why = verdict
                    break
        freshness = PUSHED_FRESHNESS_S if definition.register in self.pushed else POLLED_FRESHNESS_S
        if quality == "good" and clock.monotonic() - sample.t > freshness:
            age = durations.text(int(clock.monotonic() - sample.t))
            quality, why = "stale", f"last read {age} ago"
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

    def _identification_envelope(self, path: str) -> Envelope:
        now = _now()
        if not self._identified.done():
            return _missing(path, now, "not read yet")
        if self.identification is None:
            return _missing(path, now, "the pump doesn't answer Modbus device identification")
        text = ", ".join(f"{k}: {v}" for k, v in self.identification.items())
        return Envelope(
            point=path,
            value=text,
            unit=None,
            t_observed=None,
            t_received=now,
            quality="good",
            source="measured",
            why="what the pump says of itself; the model used is the one set",
        )

    def _snapshot(self) -> profile.Snapshot:
        """What was last read, decoded; decoded again only after a register was stored."""
        if self._decoded is None:
            model, _ = self._pump()
            values: dict[int, float | int | None] = {}
            for register, sample in self._samples.items():
                if register in model:
                    definition = model.register(register)
                    if definition.size is not None and definition.size.bits <= 16:
                        decoded = decode(definition, *sample.words, high_word_first=True)
                        values[register] = decoded.value
            self._decoded = values
        return profile.Snapshot(
            self._decoded,
            self._charge_started,
            clock.monotonic(),
            dict(self._meter_idle),
            self._returned,
        )

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

    # --- changing settings -----------------------------------------------------------------

    def _specs(self) -> dict[str, profile.Spec]:
        model, layout = self._pump()
        specs = self.family.levers(model, layout.points, self._snapshot().values)
        return {spec.path: spec for spec in specs}

    async def _act(self, request: Act, send: Send) -> None:
        spec = self._specs().get(request.lever)
        try:
            if spec is None:
                raise Refused("no such lever")
            if spec.lever.unavailable is not None:
                raise Refused(spec.lever.unavailable)
            await send(Fate(id=request.id, stage="queued", t=_now()))
            outcome = await self._carry_out(spec, request)
        except Refused as e:
            log.info("%s %s not sent: %s", request.lever, request.op, e)
            await send(Fate(id=request.id, stage="dropped", t=_now(), detail=str(e)))
            return
        await self._report(request.id, f"{request.lever} {request.op}", outcome, send)

    async def _write_setting(self, request: Write, send: Send) -> None:
        """A person's change of one of the pump's own settings: any writable register but
        the word order, and none a hold of this plugin's has engaged."""
        try:
            register = self._writable(request.point)
            await send(Fate(id=request.id, stage="queued", t=_now()))
            model, _ = self._pump()
            try:
                raw = encode(model.register(register), request.value)
            except EncodeError as e:
                raise Refused(str(e)) from None
            outcome = await self._send_write(register, raw)
        except Refused as e:
            log.info("write %s not sent: %s", request.point, e)
            await send(Fate(id=request.id, stage="dropped", t=_now(), detail=str(e)))
            return
        await self._report(request.id, f"write {request.point}", outcome, send)

    def _writable(self, point: str) -> int:
        definition = self._definition(point)
        if definition is None or not point.rpartition("/")[2].startswith("x.nibe."):
            raise Refused("not one of the pump's registers")
        register = definition.register
        if register == self.family.word_swap:
            raise Refused("the word order other clients decode by is never written")
        for path, kept in self.holds.items():
            if int(kept["register"]) == register:
                raise Refused(f"{path} holds it now")
        return register

    async def _report(self, id: int, what: str, outcome: WriteOutcome, send: Send) -> None:
        log.info(
            "%s: %d = %d, %s (%s)",
            what,
            outcome.register,
            outcome.value,
            outcome.result.value,
            outcome.why,
        )
        t = _now()
        if outcome.t_result is not None:
            t -= timedelta(seconds=max(0.0, clock.monotonic() - outcome.t_result))
        await send(Fate(id=id, stage=FATES[outcome.result], t=t, detail=outcome.why))

    async def _carry_out(self, spec: profile.Spec, request: Act) -> WriteOutcome:
        kind, op = spec.lever.kind, request.op
        if kind == "setting" and op == "set":
            value = _setting_value(spec, request.params.get("value"))
            return await self._write(spec, spec.register, value)
        if kind == "trigger" and op == "fire" and spec.fire is not None:
            return await self._write(spec, spec.register, spec.fire)
        if kind == "trigger" and op == "cancel" and spec.cancel is not None:
            return await self._write(spec, spec.register, spec.cancel)
        if kind == "hold" and op == "engage":
            return await self._engage(spec)
        if kind == "hold" and op == "release":
            return await self._release(spec)
        raise Refused(f"{spec.path} doesn't take {op}")

    async def _engage(self, spec: profile.Spec) -> WriteOutcome:
        """Engage a hold, keeping first what its release puts back. The hot-water block
        lowers the current mode's start temperature."""
        extra: dict[str, Any] = {}
        if spec.starts is not None:
            assert self.family.hot_water_mode is not None  # noqa: S101 - the block needs it
            mode = int(await self._fresh(self.family.hot_water_mode))
            register: int | None = spec.starts.get(mode)
            if register is None:
                title = profile.MODE_TITLES.get(mode, f"mode {mode}")
                raise Refused(f"{title} has no start temperature of its own to lower")
            engaged: float | None = profile.BLOCK_START
            extra["mode"] = mode
        else:
            register, engaged = spec.register, spec.held
        if register is None or engaged is None:
            raise Refused(f"{spec.path} has nothing to engage")
        kept = self.holds.get(spec.path)
        if kept is not None and kept["register"] != register:
            # Still engaged on another mode's start (its release went astray): that one is
            # put back first, or it would stay lowered with nothing to undo it.
            if _same(await self._fresh(int(kept["register"])), engaged):
                outcome = await self._release(spec)
                if outcome.result is not WriteResult.ACCEPTED:
                    return outcome
            self.holds.pop(spec.path, None)
            kept = None
        now = await self._fresh(register)
        still = kept is not None and kept["register"] == register and _same(now, engaged)
        if kept is not None and still:
            put_back = kept["value"]  # engaged before, and still: what was found then
        elif spec.starts is not None and now <= engaged:
            raise Refused(
                f"the start temperature already reads {now:g} °C, so what to put back isn't known"
            )
        else:
            put_back = now
        self.holds[spec.path] = {"register": register, "value": put_back, **extra}
        await self._save()  # before the write: a crash right after it still knows
        outcome = await self._write(spec, register, engaged)
        if outcome.result in (WriteResult.REFUSED, WriteResult.NOT_TAKEN) and not still:
            self.holds.pop(spec.path, None)
            await self._save()
        return outcome

    async def _release(self, spec: profile.Spec) -> WriteOutcome:
        kept = self.holds.get(spec.path)
        if kept is None:
            raise Refused("nothing engaged by this plugin is held here")
        outcome = await self._write(spec, int(kept["register"]), kept["value"])
        if outcome.result is WriteResult.ACCEPTED:
            self.holds.pop(spec.path, None)
            await self._save()
        return outcome

    async def _fresh(self, register: int) -> float | int:
        """A register's value, from a request the pump takes from now on."""
        model, _ = self._pump()
        words_ = await self._read(register, after=clock.monotonic())
        if words_ is None:
            raise Refused(f"{register} can't be read now")
        decoded = decode(model.register(register), *words_, high_word_first=True)
        if decoded.status is not Status.OK or decoded.value is None:
            raise Refused(f"{register} gives no good value now")
        return decoded.value

    async def _write(
        self, spec: profile.Spec, register: int | None, value: float | int
    ) -> WriteOutcome:
        """Write one of the lever's own registers; never one outside it, nor one that is
        never written."""
        model, _ = self._pump()
        if register is None or f"x.nibe.{register}" not in spec.lever.touches:
            raise Refused(f"register {register} isn't one {spec.path} changes")
        if register in profile.NEVER_WRITTEN:
            raise Refused(f"register {register} is never written")
        try:
            raw = encode(model.register(register), value)
        except EncodeError as e:
            raise Refused(str(e)) from None
        return await self._send_write(register, raw)

    async def _send_write(self, register: int, raw: int) -> WriteOutcome:
        if self._transport is None:
            raise Refused("not connected")
        frame = nibe.write_request(register, raw)
        self._mine[frame] += 1
        # Kept a while: a gateway may still send a request whose fate was unknown.
        asyncio.get_running_loop().call_later(2 * WRITE_TIMEOUT_S, self._forget, frame)
        return await self._transport.write(register, raw, timeout=WRITE_TIMEOUT_S)

    def _forget(self, frame: bytes) -> None:
        self._mine[frame] -= 1
        if self._mine[frame] <= 0:
            del self._mine[frame]

    def _spawn(self, sending: Awaitable[None]) -> None:
        task = asyncio.ensure_future(sending)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

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
                    map=f"nibe-{self.family.name}-{model.name}",
                ),
                transport=Promises(
                    fate="exact"
                    if promises is not None and promises.fate is FateKind.EXACT
                    else "best_effort",
                    ack=Ack(means="accepted", detail="0x6C: 1 accepted, 0 refused")
                    if self.family.name == "bus"
                    else Ack(means="none", detail="writing isn't built for Modbus TCP yet"),
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
        points += [
            Point(
                path=f"{unit}/{d.path}",
                unit=d.unit,
                wraps_at=None,
                resolution=Knowledge(value=d.resolution, known="documented"),
                delivery=Delivery(how="on_change"),
                validity=d.validity,
            )
            for d in layout.derived.values()
            if self._derived(f"{unit}/{d.path}") is not None
        ]
        if self.family.name == "s-series":
            points.append(
                Point(
                    path=f"{unit}/{IDENTIFICATION}",
                    label="Modbus device identification",
                    description="What the pump answers to Modbus's read device identification;"
                    " shown, not relied on.",
                    category="diagnostic",
                    delivery=Delivery(how="on_change"),
                )
            )
        values = self._snapshot().values
        if self.family.hot_water_mode is not None:
            mode = values.get(self.family.hot_water_mode)
            self._described_mode = None if mode is None else int(mode)
            self._block_described = True
        levers = [spec.lever for spec in self.family.levers(model, layout.points, values)]
        return Described(id=id, nodes=tuple(nodes), points=tuple(points), levers=tuple(levers))

    def _presence(self, path: str, kind: str) -> Presence:
        if kind == "pool":
            pool = self.family.pools[int(path.removeprefix("pool")) - 1]
            return Presence(
                how="detected",
                rule=f"{pool.accessory} = 1 and pool sensor {pool.sensor} connected",
            )
        if kind != "climate_system":
            return Presence(how="assumed")
        number = int(path.removeprefix("cs"))
        if number == 1:
            return Presence(how="detected", rule="climate system 1 is always there")
        system = self.family.systems[number - 1]
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
            category=_category(definition, register, self.family.word_swap),
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
            last = _now() - timedelta(seconds=clock.monotonic() - health.last_traffic)
        return Health(
            t=_now(),
            unit=profile.UNIT,
            state="up" if health.up else "down",
            last_traffic=last,
            counters=dict(health.detail),
        )


def _category(
    definition: profile.PointDef, register: Register, word_swap: int | None
) -> Literal["config", "diagnostic"] | None:
    """A standard point is worth seeing every day; a pump setting read back is `config`;
    any other register of the pump's own is `diagnostic`, and so is the word order."""
    if definition.register == word_swap:
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


def _setting_value(spec: profile.Spec, value: object) -> float | int:
    """What a setting is set to: a number, or one of its values by name (or the pump's
    own number for it, as a baseline may be kept)."""
    if spec.names is not None:
        if isinstance(value, str) and value in spec.names:
            return spec.names[value]
        if isinstance(value, int) and not isinstance(value, bool) and value in spec.names.values():
            return value
        raise Refused(f"{value!r} isn't one of {', '.join(spec.names)}")
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise Refused(f"{value!r} isn't a number")
    return value


def _same(a: float | int, b: float | int) -> bool:
    return abs(a - b) < 1e-6


def _now() -> datetime:
    return clock.now()


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
    return NibePlugin(
        NibeGateway.model_validate(dict(context.settings)),
        secrets=context.secrets,
        state=context.state,
    )
