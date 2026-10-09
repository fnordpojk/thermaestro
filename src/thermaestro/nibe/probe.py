"""`thermaestro probe`: a read-only check of a Nibe pump, on the bus or over Modbus TCP.

For a tester on a model Thermaestro hasn't met, or a bug report. It identifies the pump,
reads what Thermaestro reads, captures the traffic for a few minutes, and writes two files
that can become test fixtures:
- a **report** (JSON): the pump, its firmware and word order, what detection found, which
  of Thermaestro's points and levers this model's map has, and each point's value. It
  holds no address, key or name;
- a **capture** (JSON lines): on the bus, every exchange the gateway forwarded, other
  devices' traffic too; over Modbus TCP, each answer with its request. With its time.

**Read-only by construction.** The probe runs Thermaestro's own Nibe plugin, which never
writes, and gives it only `ReadOnly`, a transport with no way to send a write. Nothing
here is copied from libraries whose connectivity check writes to the pump.
"""

import asyncio
import json
import tempfile
import time
from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from thermaestro_gateway import nibe
from thermaestro_gateway.protocol import Origin

from .. import durations, version
from ..cap import Link, pair, serve
from ..cap.messages import Described
from ..store import NibeGateway, SecretStore
from . import profile
from .maps import RegisterMap, Status, decode, words
from .plugin import NibePlugin
from .transport import GatewayConfig, ModbusConfig, connect
from .transport.base import (
    LinkHealth,
    Observed,
    Promises,
    ReadFailed,
    Reading,
    Transport,
    WriteOutcome,
)

FORMAT = "thermaestro-probe"
CAPTURE_FORMAT = "thermaestro-capture"
VERSION = 1
SECONDS = 300.0
UNREAD_GRACE_S = 120.0
DETECTION_READ_S = 60.0
"""How long a detection read may take: on a real pump it queues behind the plugin's polling,
about one read a second."""
PSK_NAME = "probe.psk"


class ProbeFailed(Exception):
    pass


class ReadOnly:
    """A transport that reads and listens, and has no way to write: the probe's only path
    to the pump."""

    def __init__(self, inner: Transport) -> None:
        self._inner = inner

    @property
    def promises(self) -> Promises:
        return self._inner.promises

    async def read(
        self, register: int, *, after: float | None = None, timeout: float = 30.0
    ) -> Reading:
        return await self._inner.read(register, after=after, timeout=timeout)

    async def write(self, register: int, value: int, *, timeout: float = 30.0) -> WriteOutcome:
        raise PermissionError(f"the probe never writes (register {register})")

    async def identify(self) -> dict[str, str] | None:
        """Modbus's device identification, where the transport has it: a read."""
        identify = getattr(self._inner, "identify", None)
        return None if identify is None else await identify()

    def observe(self, callback: Callable[[Observed], None]) -> Callable[[], None]:
        return self._inner.observe(callback)

    def health(self) -> LinkHealth:
        return self._inner.health()

    async def close(self) -> None:
        await self._inner.close()


@dataclass
class Capture:
    """Every forwarded bus exchange, with its time in seconds from the start."""

    started: float = field(default_factory=time.monotonic)
    lines: list[dict[str, Any]] = field(default_factory=list)

    def add(self, observed: Observed) -> None:
        line: dict[str, Any] = {
            "t": round(observed.t - self.started, 4),
            "data": observed.data.hex(),
            "reply": observed.reply.hex(),
            "trailer": observed.trailer.hex(),
        }
        if observed.origin is not None:
            line["origin"] = observed.origin.name.lower()
        self.lines.append(line)

    def text(self, header: dict[str, Any]) -> str:
        head = {"format": CAPTURE_FORMAT, "version": VERSION, **header}
        return "".join(json.dumps(line) + "\n" for line in (head, *self.lines))


def load_capture(path: Path) -> tuple[dict[str, Any], list[Observed]]:
    """A capture's header and its exchanges, as a transport would have reported them."""
    lines = path.read_text(encoding="utf-8").splitlines()
    header = json.loads(lines[0])
    if header.get("format") != CAPTURE_FORMAT:
        raise ValueError(f"{path} isn't a {CAPTURE_FORMAT} file")
    out = []
    for raw in lines[1:]:
        line = json.loads(raw)
        data = bytes.fromhex(line["data"])
        try:
            telegram: nibe.Telegram | None = nibe.parse_telegram(nibe.split_exchange(data).telegram)
        except nibe.FrameError:
            telegram = None
        origin = Origin[line["origin"].upper()] if "origin" in line else None
        out.append(
            Observed(
                data,
                telegram,
                bytes.fromhex(line["reply"]),
                bytes.fromhex(line["trailer"]),
                line["t"],
                origin,
            )
        )
    return header, out


@dataclass
class Result:
    report: dict[str, Any]
    capture: Capture


async def probe(
    gateway: NibeGateway,
    *,
    psk: bytes | None = None,
    seconds: float = SECONDS,
    maps: RegisterMap | None = None,
    connect_fn: Callable[..., Awaitable[Transport]] = connect,
    transport_settings: dict[str, Any] | None = None,
    identify_timeout_s: float = 40.0,
    detection_read_s: float = DETECTION_READ_S,
    progress: Callable[[str], None] = lambda _: None,
) -> Result:
    """Identify the pump, read Thermaestro's points while capturing the bus for
    `seconds`, and make the report."""
    capture = Capture()
    transports: list[ReadOnly] = []

    async def read_only(config: GatewayConfig | ModbusConfig, **settings: Any) -> Transport:
        transport = ReadOnly(await connect_fn(config, **settings))
        transport.observe(capture.add)
        transports.append(transport)
        return transport

    with tempfile.TemporaryDirectory() as tmp:
        secrets = SecretStore(Path(tmp) / "secrets.json")
        if psk is not None:
            await secrets.set(PSK_NAME, psk.hex())
            gateway = gateway.model_copy(update={"psk": PSK_NAME})
        plugin = NibePlugin(
            gateway,
            secrets=secrets,
            maps=maps,
            connect_fn=read_only,
            transport_settings=transport_settings,
            identify_timeout_s=identify_timeout_s,
        )
        core, plugin_side = pair()
        served = asyncio.create_task(serve(plugin_side, plugin))
        link = Link(core)
        link.start()
        try:
            await link.hello(timeout=10)
            described = await _identified(link, plugin, identify_timeout_s + 30)
            model = plugin.model
            assert model is not None  # noqa: S101 - described means identified
            progress(f"Identified the pump: {model.name}, firmware {plugin.firmware}.")
            detection = await _detection(transports[0], plugin, detection_read_s)
            progress(f"Reading and capturing the bus for {durations.text(seconds)}.")
            started = time.monotonic()
            while (left := seconds - (time.monotonic() - started)) > 0:
                await asyncio.sleep(min(60.0, left))
                if left > 60:
                    progress(f"{len(capture.lines)} exchanges so far.")
            # Every point read once, within as long again (polled points come about one a
            # second, so a short capture may not have reached them all).
            paths = [p.path for p in described.points]
            until = time.monotonic() + seconds + UNREAD_GRACE_S
            while time.monotonic() < until and any(_unread(plugin, path) for path in paths):
                await asyncio.sleep(0.2)
            values = await link.read(paths, timeout=60)
            health = transports[0].health()
        finally:
            await link.close()
            await plugin_side.close()
            served.cancel()
            await asyncio.gather(served, return_exceptions=True)
    report = _report(plugin, described, values.values, detection, capture, health, seconds)
    return Result(report, capture)


async def _identified(link: Link, plugin: NibePlugin, wait_s: float) -> Described:
    """The pump's description, or why it couldn't be identified as soon as that's known."""
    describing = asyncio.create_task(link.describe(timeout=wait_s))
    while not describing.done():
        if plugin.problem is not None:
            describing.cancel()
            await asyncio.gather(describing, return_exceptions=True)
            raise ProbeFailed(plugin.problem)
        await asyncio.wait({describing}, timeout=0.2)
    try:
        return describing.result()
    except Exception as e:  # noqa: BLE001 - any failure here is "not identified"
        raise ProbeFailed(plugin.problem or f"the pump wasn't identified ({e})") from None


def _unread(plugin: NibePlugin, path: str) -> bool:
    return plugin.envelope(path).why == "not read yet"


async def _detection(
    transport: ReadOnly, plugin: NibePlugin, timeout: float
) -> list[dict[str, Any]]:
    """Each climate system beyond the first this model can have: its accessory switch and
    whether its supply sensor is connected, read again for the report."""
    model = plugin.model
    assert model is not None  # noqa: S101
    out = []
    for system in plugin.family.detectable(model):
        entry: dict[str, Any] = {"system": system.number, "supply": system.supply}
        if system.accessory is not None:
            entry["accessory"] = system.accessory
            found = await _read(transport, system.accessory, timeout)
            entry["accessory_value"] = (
                None
                if found is None
                else decode(model.register(system.accessory), *found, high_word_first=True).value
            )
        found = await _read(transport, system.supply, timeout)
        entry["supply_connected"] = (
            None
            if found is None
            else decode(model.register(system.supply), *found, high_word_first=True).status
            is not Status.NOT_CONNECTED
        )
        entry["detected"] = plugin.layout is not None and system.number in plugin.layout.systems
        out.append(entry)
    return out


async def _read(transport: ReadOnly, register: int, timeout: float) -> tuple[int, int] | None:
    try:
        return words((await transport.read(register, timeout=timeout)).data)
    except ReadFailed:
        return None


def _report(
    plugin: NibePlugin,
    described: Described,
    values: tuple[Any, ...],
    detection: list[dict[str, Any]],
    capture: Capture,
    health: LinkHealth,
    seconds: float,
) -> dict[str, Any]:
    model, layout = plugin.model, plugin.layout
    assert model is not None  # noqa: S101
    assert layout is not None  # noqa: S101
    unit = described.nodes[0]
    envelopes = {e.point: e for e in values}
    points = []
    for point in described.points:
        below = point.path.removeprefix(f"{profile.UNIT}/")
        definition = layout.points.get(below)
        envelope = envelopes.get(point.path)
        points.append(
            {
                "path": point.path,
                "register": definition.register if definition else None,
                "label": point.label,
                "category": point.category,
                "value": envelope.value if envelope else None,
                "unit": envelope.unit if envelope else point.unit,
                "raw": envelope.raw if envelope else None,
                "quality": envelope.quality if envelope else "unknown",
                "why": envelope.why if envelope else "not read",
            }
        )
    family = plugin.family
    missing = [
        {"path": f"{profile.UNIT}/{p.path}", "register": p.register}
        for _, _, defs in family.groups(
            range(1, len(family.systems) + 1), range(1, len(family.pools) + 1)
        )
        for p in defs
        if p.register not in model
    ]
    offered = {lever.path: lever for lever in described.levers}
    exchanges = Counter(
        f"0x{t.address:02x} 0x{t.command:02x}"
        for line in capture.lines
        if family.name == "bus" and (t := _telegram(line["data"])) is not None
    )
    swap = plugin.high_word_first
    return {
        "format": FORMAT,
        "version": VERSION,
        "thermaestro": version(),
        "made": datetime.now(UTC).isoformat(timespec="seconds"),
        "seconds": seconds,
        "transport": {
            "protocol": health.protocol,
            "counters": dict(sorted(health.detail.items())),
        },
        "pump": {
            "family": family.name,
            "product": unit.label,
            "model": model.name,
            "firmware": plugin.firmware,
            "word_order": None if swap is None else ("high first" if swap else "low first"),
            "identification": plugin.identification,
        },
        "detection": {
            "climate_systems": layout.systems,
            "pools": layout.pools,
            "checked": detection,
            "nodes": sorted(layout.nodes),
        },
        "points": points,
        "missing": missing,
        "levers": [
            {"path": path, "kind": lever.kind, "registers": list(lever.touches)}
            for path, lever in sorted(offered.items())
        ],
        "levers_missing": [
            f"{profile.UNIT}/{p}"
            for p in family.lever_paths
            if f"{profile.UNIT}/{p}" not in offered
        ],
        "pushed": sorted(plugin.pushed),
        "absent": sorted(plugin.absent),
        "bus": {"exchanges": len(capture.lines), "by_address_and_command": dict(exchanges)},
    }


def _telegram(data: str) -> nibe.Telegram | None:
    try:
        return nibe.parse_telegram(nibe.split_exchange(bytes.fromhex(data)).telegram)
    except nibe.FrameError:
        return None
