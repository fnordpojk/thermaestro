"""The gateway's I/O: the serial port, the plain NibeGW ports and the control port.

Everything that decides lives in bus.py, engine.py and control.py; this module only
moves bytes and keeps time. Replies to the pump go out as soon as the bus state machine
produces them, from the serial port's read callback, so the network never delays them
(docs/gateway-protocol.md §1).
"""

import asyncio
import contextlib
import functools
import logging
import time
from dataclasses import dataclass, field

import serial

from thermaestro_gateway import nibe
from thermaestro_gateway.bus import Bus
from thermaestro_gateway.control import Address, Control, Datagram, Settings
from thermaestro_gateway.engine import Engine

log = logging.getLogger(__name__)

BAUD = 9600
TARGET_LEASE_US = 120_000_000
TICK_S = 0.1


@dataclass(frozen=True, slots=True)
class Config:
    serial_port: str
    listen: str = "0.0.0.0"  # noqa: S104  # a gateway serves the LAN
    read_port: int | None = 9999
    """The plain NibeGW read port; None disables it, 0 picks a free port (tests)."""
    write_port: int | None = 10000
    control_port: int | None = 10090
    acknowledged: frozenset[int] = frozenset({nibe.MODBUS40})
    constants: dict[tuple[int, int], bytes] = field(default_factory=dict)
    sources: frozenset[str] | None = None
    static_targets: tuple[Address, ...] = ()
    psk: bytes | None = None
    max_clients: int = 4
    queue_cap: int = 3


def now_us() -> int:
    return time.monotonic_ns() // 1000


class _Port(asyncio.DatagramProtocol):
    def __init__(self, gateway: "Gateway", name: str) -> None:
        self.gateway = gateway
        self.name = name
        self.transport: asyncio.DatagramTransport | None = None

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        if isinstance(transport, asyncio.DatagramTransport):
            self.transport = transport

    def datagram_received(self, data: bytes, addr: Address) -> None:
        self.gateway.datagram(self.name, data, addr)

    def error_received(self, exc: Exception) -> None:
        log.warning("UDP error on the %s port: %s", self.name, exc)
        self.gateway.control.stats.udp_send_errors += 1

    def send(self, data: bytes, addr: Address) -> None:
        if self.transport is not None:
            self.transport.sendto(data, addr)


class Gateway:
    def __init__(self, config: Config) -> None:
        self.config = config
        keys = {(nibe.MODBUS40, nibe.READ_TOKEN), (nibe.MODBUS40, nibe.WRITE_TOKEN)}
        self.engine = Engine(keys=keys, queue_cap=config.queue_cap, constants=config.constants)
        self.bus = Bus(acknowledged=config.acknowledged, responder=self.engine)
        self.control = Control(
            self.engine,
            self.bus.stats,
            Settings(
                psk=config.psk,
                plain_ports=(config.read_port or 0, config.write_port or 0),
                max_clients=config.max_clients,
                sources=config.sources,
                acknowledged=config.acknowledged,
                started_us=now_us(),
            ),
        )
        self.ports: dict[str, int] = {}
        self._udp: dict[str, _Port] = {}
        self._targets: dict[Address, int | None] = dict.fromkeys(config.static_targets)
        self._serial: serial.Serial | None = None
        self._stopping = asyncio.Event()

    async def run(self) -> None:
        """Run until stop() is called."""
        await self.start()
        try:
            await self._stopping.wait()
        finally:
            await self.close()

    async def start(self) -> None:
        loop = asyncio.get_running_loop()
        # The serial port first: if it can't be opened, nothing else has been.
        self._serial = serial.Serial(self.config.serial_port, BAUD, timeout=0, write_timeout=1)
        for name, port in (
            ("read", self.config.read_port),
            ("write", self.config.write_port),
            ("control", self.config.control_port),
        ):
            if port is None:
                continue
            transport, protocol = await loop.create_datagram_endpoint(
                functools.partial(_Port, self, name), local_addr=(self.config.listen, port)
            )
            self._udp[name] = protocol
            self.ports[name] = transport.get_extra_info("sockname")[1]
        _warn_about_latency(self.config.serial_port)
        loop.add_reader(self._serial.fileno(), self._serial_readable, self._serial)
        self._ticker = asyncio.create_task(self._tick())
        log.info("gateway on %s, ports %s", self.config.serial_port, self.ports)

    def stop(self) -> None:
        self._stopping.set()

    async def close(self) -> None:
        self._ticker.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._ticker
        self._send_control(self.control.shutdown(now_us()))
        if self._serial is not None:
            asyncio.get_running_loop().remove_reader(self._serial.fileno())
            self._serial.close()
        for port in self._udp.values():
            if port.transport is not None:
                port.transport.close()

    # --- in ------------------------------------------------------------------------------

    def _serial_readable(self, port: serial.Serial) -> None:
        for byte in port.read(port.in_waiting or 1):
            t = now_us()
            step = self.bus.feed(byte, t)
            if step.write:
                port.write(step.write)
                self.control.note_reply_prep(now_us() - t)
            if step.done is not None:
                self._forward_plain(step.done.data, t)
                self._send_control(self.control.on_exchange(step.done))

    def _forward_plain(self, data: bytes, now: int) -> None:
        """Every exchange goes to every plain target, from the read port, as on esphome-nibe."""
        read_port = self._udp.get("read")
        if read_port is not None:
            for target in self._live_targets(now):
                read_port.send(data, target)

    def datagram(self, port: str, data: bytes, addr: Address) -> None:
        if port == "control":
            self._send_control(self.control.handle(data, addr, now_us()))
            return
        sources = self.config.sources
        if sources is not None and addr[0] not in sources:
            return
        token = nibe.READ_TOKEN if port == "read" else nibe.WRITE_TOKEN
        t = now_us()
        if not self.engine.submit_plain(nibe.MODBUS40, token, data, t):
            log.debug("plain request from %s refused: %s", addr, data.hex())
        if addr not in self._targets or self._targets[addr] is not None:
            self._targets[addr] = t + TARGET_LEASE_US

    # --- out -----------------------------------------------------------------------------

    def _send_control(self, datagrams: list[Datagram]) -> None:
        port = self._udp.get("control")
        for addr, data in datagrams:
            if port is None:
                self.control.stats.events_dropped += 1
            else:
                port.send(data, addr)

    def _live_targets(self, now: int) -> list[Address]:
        return [a for a, until in self._targets.items() if until is None or until > now]

    async def _tick(self) -> None:
        last = time.monotonic()
        while True:
            await asyncio.sleep(TICK_S)
            current = time.monotonic()
            # How late this pass is shows how long the loop was blocked (LOOP_GAP_MAX_MS).
            self.control.note_loop_gap(int((current - last) * 1000))
            last = current
            t = now_us()
            self._targets = {
                a: until for a, until in self._targets.items() if until is None or until > t
            }
            self._send_control(self.control.tick(t))


def _warn_about_latency(path: str) -> None:
    """FTDI adapters buffer received bytes for 16 ms by default, longer than a reply may
    take; 1 ms is what a gateway wants."""
    name = path.rsplit("/", 1)[-1]
    timer = f"/sys/bus/usb-serial/devices/{name}/latency_timer"
    with contextlib.suppress(OSError, ValueError):
        with open(timer) as f:
            ms = int(f.read().strip())
        if ms > 1:
            log.warning("%s has latency_timer %d ms; set it to 1 (%s)", path, ms, timer)
