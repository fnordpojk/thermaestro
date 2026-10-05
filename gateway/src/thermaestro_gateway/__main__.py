"""thermaestro-gateway: a Nibe MODBUS40 gateway for an RS485 adapter on a Linux machine.

It speaks plain NibeGW on UDP (as esphome-nibe does) and the Thermaestro gateway protocol
on its control port.
"""

import argparse
import asyncio
import logging
import signal
import sys
from pathlib import Path

import serial

from thermaestro_gateway import nibe
from thermaestro_gateway.server import Config, Gateway


def _int(text: str) -> int:
    return int(text, 0)


def _constant(text: str) -> tuple[tuple[int, int], bytes]:
    address, token, data = text.split(":")
    return (int(address, 0), int(token, 0)), bytes.fromhex(data)


def _target(text: str) -> tuple[str, int]:
    host, port = text.rsplit(":", 1)
    return host, int(port)


def _port(value: int) -> int | None:
    return value or None


def parse(argv: list[str] | None = None) -> tuple[Config, str]:
    ap = argparse.ArgumentParser(prog="thermaestro-gateway", description=__doc__)
    ap.add_argument("serial_port", help="the RS485 adapter, e.g. /dev/ttyUSB0 or /dev/serial0")
    ap.add_argument("--listen", default="0.0.0.0", help="address to listen on")  # noqa: S104
    ap.add_argument("--read-port", type=int, default=9999, help="plain NibeGW read port, 0 = off")
    ap.add_argument("--write-port", type=int, default=10000, help="plain write port, 0 = off")
    ap.add_argument("--control-port", type=int, default=10090, help="control port, 0 = off")
    ap.add_argument(
        "--acknowledge",
        type=_int,
        action="append",
        metavar="ADDRESS",
        help="a bus address to answer as (repeatable; default 0x20, MODBUS40)",
    )
    ap.add_argument(
        "--constant",
        type=_constant,
        action="append",
        default=[],
        metavar="ADDRESS:TOKEN:HEX",
        help="answer a token with fixed data, e.g. 0x20:0xee:0a0001 (repeatable)",
    )
    ap.add_argument(
        "--no-default-constants",
        action="store_true",
        help="don't answer MODBUS40's accessory token with 0a0001",
    )
    ap.add_argument(
        "--source", action="append", metavar="IP", help="only accept requests from these"
    )
    ap.add_argument(
        "--target",
        type=_target,
        action="append",
        default=[],
        metavar="IP:PORT",
        help="always forward bus traffic here (repeatable)",
    )
    ap.add_argument(
        "--psk-file",
        type=Path,
        help="file holding the control port's pre-shared key, 64 hex digits",
    )
    ap.add_argument("--max-clients", type=int, default=4)
    ap.add_argument("--log-level", default="INFO")
    args = ap.parse_args(argv)

    constants = (
        {}
        if args.no_default_constants
        else {(nibe.MODBUS40, nibe.ACCESSORY_TOKEN): nibe.MODBUS40_ACCESSORY_REPLY}
    )
    constants.update(dict(args.constant))
    psk = None
    if args.psk_file is not None:
        psk = bytes.fromhex(args.psk_file.read_text().strip())
        if len(psk) != 32:
            ap.error(f"{args.psk_file} must hold 32 bytes as 64 hex digits")
    config = Config(
        serial_port=args.serial_port,
        listen=args.listen,
        read_port=_port(args.read_port),
        write_port=_port(args.write_port),
        control_port=_port(args.control_port),
        acknowledged=frozenset(args.acknowledge or [nibe.MODBUS40]),
        constants=constants,
        sources=frozenset(args.source) if args.source else None,
        static_targets=tuple(args.target),
        psk=psk,
        max_clients=args.max_clients,
    )
    return config, args.log_level


async def _serve(config: Config) -> None:
    gateway = Gateway(config)
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, gateway.stop)
    await gateway.run()


def main(argv: list[str] | None = None) -> None:
    config, level = parse(argv)
    logging.basicConfig(level=level, format="%(levelname)s %(name)s: %(message)s")
    try:
        asyncio.run(_serve(config))
    except (serial.SerialException, OSError) as e:
        sys.exit(f"thermaestro-gateway: {e}")


if __name__ == "__main__":
    main()
