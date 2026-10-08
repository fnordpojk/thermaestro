"""The Nibe plugin's transports: plain NibeGW and the Thermaestro gateway protocol for the
bus family, and Modbus TCP for the S-series.

For a gateway, `connect()` makes the choice of docs/gateway-protocol.md §13: the protocol
where the gateway speaks it, otherwise plain NibeGW. It falls back only when the control
port is silent or speaks another version, and never when a key is configured: a
configured key asks for authenticated access, which the plain ports can't give.
"""

import logging
from dataclasses import dataclass

from thermaestro.nibe.transport.base import Transport
from thermaestro.nibe.transport.modbus import ModbusTransport
from thermaestro.nibe.transport.nibegw import PlainClient, PlainSettings
from thermaestro.nibe.transport.tgw import HandshakeFailed, HandshakeIssue, TgwClient, TgwSettings

log = logging.getLogger(__name__)

FALLBACK = frozenset((HandshakeIssue.SILENT, HandshakeIssue.VERSION))


@dataclass(frozen=True, slots=True)
class GatewayConfig:
    host: str
    read_port: int = 9999
    write_port: int = 10000
    control_port: int | None = 10090
    """None: the gateway is known not to speak the protocol."""
    psk: bytes | None = None

    def __post_init__(self) -> None:
        if self.psk is not None and self.control_port is None:
            raise ValueError("a gateway key is only used on the control port")


@dataclass(frozen=True, slots=True)
class ModbusConfig:
    """An S-series pump's own Modbus TCP server."""

    host: str
    port: int = 502
    wide: frozenset[int] = frozenset()
    """The model's 32-bit registers, each read as two."""


async def connect(
    config: GatewayConfig | ModbusConfig,
    *,
    tgw_settings: TgwSettings | None = None,
    plain_settings: PlainSettings | None = None,
) -> Transport:
    """A started transport to the gateway or the pump. Raises HandshakeFailed where spec §13
    says to stop, and OSError where an S-series pump doesn't answer."""
    if isinstance(config, ModbusConfig):
        modbus = ModbusTransport(config.host, config.port, wide=config.wide)
        try:
            await modbus.start()
        except OSError:
            await modbus.close()
            raise
        return modbus
    if config.control_port is not None:
        client = TgwClient(config.host, config.control_port, psk=config.psk, settings=tgw_settings)
        try:
            await client.start()
        except HandshakeFailed as e:
            await client.close()
            if config.psk is not None or e.issue not in FALLBACK:
                raise
            log.warning("%s:%s: %s; using plain NibeGW", config.host, config.control_port, e)
        else:
            return client
    plain = PlainClient(config.host, config.read_port, config.write_port, settings=plain_settings)
    await plain.start()
    return plain
