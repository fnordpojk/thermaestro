"""`thermaestro probe` against the simulated pump: what it reports, that it writes
nothing, that the report names no address or key, and that its capture reads back."""

import asyncio
import inspect
import itertools
import json
from pathlib import Path
from typing import Any

import pytest
from simpump import TEST_PSK, SimPump
from test_nibe_plugin import FAST_PLAIN, FAST_TGW, PUMP
from thermaestro_gateway.server import Gateway

from thermaestro.cli import main
from thermaestro.nibe import probe as probe_module
from thermaestro.nibe import profile
from thermaestro.nibe.probe import ProbeFailed, ReadOnly, load_capture, probe
from thermaestro.nibe.transport import GatewayConfig, connect
from thermaestro.nibe.transport.base import (
    LinkHealth,
    Promises,
    ReadFailed,
    Reading,
    Transport,
    WriteOutcome,
)
from thermaestro.store import NibeGateway

LOG_SET = [40004, 40008, 43005]


@pytest.fixture
def stocked(pump: SimPump) -> SimPump:
    pump.registers.update(PUMP)
    pump.registers32[42437] = 12_345
    pump.registers32[42439] = 0xFFFF_FFFF
    pump.info_interval_s = 0.2
    pump.log_set = LOG_SET
    return pump


def plain(gateway: Gateway, **kw: Any) -> NibeGateway:
    return NibeGateway(
        host="127.0.0.1",
        read_port=gateway.ports["read"],
        write_port=gateway.ports["write"],
        **kw,
    )


async def test_the_report(stocked: SimPump, gateway: Gateway) -> None:
    result = await probe(
        plain(gateway),
        seconds=1.0,
        transport_settings={"plain_settings": FAST_PLAIN},
        identify_timeout_s=5,
    )
    report = result.report
    assert (report["format"], report["version"]) == ("thermaestro-probe", 1)
    assert report["pump"] == {
        "product": "F1245-6 CU",
        "model": "F1245",
        "firmware": "9721R4",
        "word_order": "high first",
    }
    assert report["transport"]["protocol"] == "nibegw"
    assert report["detection"]["climate_systems"] == [1]
    system2 = next(c for c in report["detection"]["checked"] if c["system"] == 2)
    assert system2 == {
        "system": 2,
        "supply": 40007,
        "accessory": 47302,
        "accessory_value": 0,
        "supply_connected": False,
        "detected": False,
    }
    points = {p["path"]: p for p in report["points"]}
    outdoor = points["hp1/outdoor.temp"]
    assert (outdoor["value"], outdoor["unit"], outdoor["quality"], outdoor["register"]) == (
        -6.0,
        "degC",
        "good",
        40004,
    )
    assert points["hp1/dhw/temp.top"]["value"] == 51.0
    assert not [p for p in report["points"] if p["why"] == "not read yet"]
    assert all(m["register"] not in PUMP for m in report["missing"])
    assert {lever["path"] for lever in report["levers"]} | set(report["levers_missing"]) == {
        f"hp1/{p}" for p in profile.LEVERS
    }
    assert report["absent"] == [42439]  # the meter this pump doesn't keep
    assert set(LOG_SET) <= set(report["pushed"])
    assert any(k.endswith(" 0x6d") for k in report["bus"]["by_address_and_command"])
    assert stocked.taken_writes == []


async def test_over_the_gateway_protocol_naming_no_address_or_key(
    stocked: SimPump, gateway_with_psk: Gateway
) -> None:
    result = await probe(
        NibeGateway(
            host="127.0.0.1",
            protocol="thermaestro-gw",
            control_port=gateway_with_psk.ports["control"],
            psk="ignored",
        ),
        psk=TEST_PSK,
        seconds=1.0,
        transport_settings={"tgw_settings": FAST_TGW},
        identify_timeout_s=5,
    )
    assert result.report["transport"]["protocol"] == "thermaestro-gw"
    text = json.dumps(result.report) + result.capture.text({})
    assert "127.0.0.1" not in text
    assert TEST_PSK.hex() not in text
    assert result.report["pump"]["model"] == "F1245"
    assert stocked.taken_writes == []


async def test_the_capture_reads_back(stocked: SimPump, gateway: Gateway, tmp_path: Path) -> None:
    result = await probe(
        plain(gateway),
        seconds=1.0,
        transport_settings={"plain_settings": FAST_PLAIN},
        identify_timeout_s=5,
    )
    path = tmp_path / "capture.jsonl"
    path.write_text(result.capture.text({"model": "F1245"}))
    header, exchanges = load_capture(path)
    assert header["format"] == "thermaestro-capture"
    assert len(exchanges) == len(result.capture.lines) > 0
    pushed = {
        int.from_bytes(t.payload[i : i + 2], "little")
        for e in exchanges
        if (t := e.telegram) is not None and t.command == 0x68
        for i in range(0, len(t.payload) - 3, 4)
    }
    assert pushed - {0xFFFF} == set(result.report["pushed"])
    assert all(a.t <= b.t for a, b in itertools.pairwise(exchanges))


async def test_an_unknown_pump_is_reported(stocked: SimPump, gateway: Gateway) -> None:
    stocked.product = "Something else"
    with pytest.raises(ProbeFailed, match="no register map for the product 'Something else'"):
        await probe(
            plain(gateway),
            seconds=0.1,
            transport_settings={"plain_settings": FAST_PLAIN},
            identify_timeout_s=3,
        )


class Queued(ReadOnly):
    """Reads of one register wait, as on a real pump, where they queue behind polling."""

    async def read(
        self, register: int, *, after: float | None = None, timeout: float = 30.0
    ) -> Reading:
        if register == 40007:
            if timeout < 0.5:
                await asyncio.sleep(timeout)
                raise ReadFailed(register, "timed out in the queue")
            await asyncio.sleep(0.5)
        return await super().read(register, after=after, timeout=timeout)


async def test_detection_waits_for_queued_reads(stocked: SimPump, gateway: Gateway) -> None:
    async def queued(config: GatewayConfig, **settings: Any) -> Transport:
        return Queued(await connect(config, **settings))

    def system2(**kw: Any) -> Any:
        return probe(
            plain(gateway),
            seconds=1.0,
            connect_fn=queued,
            transport_settings={"plain_settings": FAST_PLAIN},
            identify_timeout_s=5,
            **kw,
        )

    hurried = await system2(detection_read_s=0.2)
    checked = next(c for c in hurried.report["detection"]["checked"] if c["system"] == 2)
    assert checked["supply_connected"] is None  # not read in time
    patient = await system2()
    checked = next(c for c in patient.report["detection"]["checked"] if c["system"] == 2)
    assert checked["supply_connected"] is False
    assert stocked.taken_writes == []


class Inner:
    """A transport that records any write it is given."""

    def __init__(self) -> None:
        self.writes: list[tuple[int, int]] = []

    @property
    def promises(self) -> Promises:
        raise NotImplementedError

    async def read(
        self, register: int, *, after: float | None = None, timeout: float = 30.0
    ) -> Reading:
        raise NotImplementedError

    async def write(self, register: int, value: int, *, timeout: float = 30.0) -> WriteOutcome:
        self.writes.append((register, value))
        raise NotImplementedError

    def observe(self, callback: Any) -> Any:
        return lambda: None

    def health(self) -> LinkHealth:
        raise NotImplementedError

    async def close(self) -> None:
        pass


async def test_the_probe_has_no_way_to_write() -> None:
    inner = Inner()
    with pytest.raises(PermissionError):
        await ReadOnly(inner).write(45171, 0)
    assert inner.writes == []
    # And nothing in the probe asks a transport to write.
    source = inspect.getsource(probe_module)
    assert ".write(" not in source


async def test_the_command(stocked: SimPump, gateway: Gateway, tmp_path: Path) -> None:
    out = tmp_path / "out"
    argv = [
        "probe",
        "127.0.0.1",
        "--read-port",
        str(gateway.ports["read"]),
        "--minutes",
        "0.02",
        "--out",
        str(out),
    ]
    assert await asyncio.to_thread(main, argv) == 0
    written = sorted(p.name for p in out.iterdir())
    assert len(written) == 2
    report_file = next(name for name in written if name.endswith(".json"))
    assert json.loads((out / report_file).read_text())["pump"]["model"] == "F1245"
    assert report_file.replace(".json", "-capture.jsonl") in written
    assert stocked.taken_writes == []
    # The gateway protocol needs its key.
    no_key = ["probe", "127.0.0.1", "--protocol", "thermaestro-gw"]
    assert await asyncio.to_thread(main, no_key) == 2
