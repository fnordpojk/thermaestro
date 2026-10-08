"""The Nibe plugin against the simulated pump, through the Python gateway, as a core
sees it: identification, detection, values and their quality, and nothing written."""

import asyncio
import contextlib
from collections.abc import AsyncIterator, Callable
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from simpump import TEST_PSK, SimPump
from thermaestro_gateway.server import Gateway

from thermaestro.cap import Link, pair, serve
from thermaestro.cap.conformance import run
from thermaestro.cap.messages import Described, DeviceEvent
from thermaestro.core import discover
from thermaestro.nibe import logset, profile
from thermaestro.nibe.maps import load
from thermaestro.nibe.plugin import NibePlugin, NotIdentified, model_for
from thermaestro.nibe.transport.nibegw import PlainSettings
from thermaestro.nibe.transport.tgw import TgwSettings
from thermaestro.store import NibeGateway, SecretStore

FAST_PLAIN = PlainSettings(resend_s=0.5, answer_s=0.5, silent_s=1.0, tick_s=0.05)
FAST_TGW = TgwSettings(
    hello_wait_s=0.3,
    fate_wait_s=0.5,
    answer_timeout_ms=500,
    retry_s=0.1,
    rehello_s=0.2,
    tick_s=0.05,
    health_interval_s=1,
    lease_s=10,
)

PUMP = {
    40004: 0xFFC4,  # BT1 -6.0 °C
    40008: 350,  # BT2 35.0
    40012: 300,  # BT3 30.0
    40013: 510,  # BT7 51.0
    40014: 480,  # BT6 48.0
    40015: 20,  # BT10 2.0
    40016: 0x8000,  # BT11: not connected
    40007: 0x8000,  # S2 supply: not connected
    40006: 0x8000,
    40005: 0x8000,
    43086: 30,  # prio: heating
    43427: 60,  # compressor running
    43431: 20,
    43433: 20,
    43437: 50,  # supply pump 50 %
    43439: 40,
    43084: 0,
    43005: 0xFF20,  # degree minutes -22.4
    45001: 0,
    43001: 9721,
    44331: 4,
    48852: 0,  # 32-bit values high word first
    47011: 0xFFFC,  # offset -4
    47041: 1,
}


@pytest.fixture
def stocked(pump: SimPump) -> SimPump:
    pump.registers.update(PUMP)
    pump.registers32[42437] = 12_345  # heat meter, hot water: 1234.5 kWh
    pump.registers32[42439] = 0xFFFF_FFFF  # a heat meter the pump doesn't keep
    pump.info_interval_s = 0.2
    return pump


def settings(gateway: Gateway, **kw: Any) -> NibeGateway:
    return NibeGateway(
        host="127.0.0.1",
        read_port=gateway.ports["read"],
        write_port=gateway.ports["write"],
        **kw,
    )


@pytest.fixture
async def plugin(stocked: SimPump, gateway: Gateway) -> AsyncIterator[NibePlugin]:
    p = NibePlugin(
        settings(gateway),
        transport_settings={"plain_settings": FAST_PLAIN},
        identify_timeout_s=5,
        health_interval_s=0.5,
    )
    async with running_plugin(p) as running:
        yield running


@contextlib.asynccontextmanager
async def running_plugin(
    p: NibePlugin, events: list[object] | None = None
) -> AsyncIterator[NibePlugin]:
    """The plugin served, as the core would; `events` collects what it sends unasked."""
    core, plugin_side = pair()
    served = asyncio.create_task(serve(plugin_side, p))
    link = Link(core, on_event=events.append if events is not None else None)
    link.start()
    try:
        await link.hello(timeout=5)
        await link.describe(timeout=15)  # waits for identification
        yield p
    finally:
        await link.close()
        await plugin_side.close()
        served.cancel()
        await asyncio.gather(served, return_exceptions=True)


async def until(condition: Callable[[], bool], timeout: float = 10.0) -> None:
    async with asyncio.timeout(timeout):
        while not condition():
            await asyncio.sleep(0.05)


def value(p: NibePlugin, point: str) -> tuple[object, str, str | None]:
    e = p.envelope(f"hp1/{point}")
    return e.value, e.quality, e.why


def all_read(p: NibePlugin) -> bool:
    _, layout = p._pump()
    return all(value(p, path)[2] != "not read yet" for path in layout.points)


def test_product_names_resolve_to_maps() -> None:
    maps = load("bus")
    assert model_for("F1245-6 CU", maps) == "F1245"
    assert model_for("VVM 320 E", maps) == "VVM320"
    assert model_for("SMO 40", maps) == "SMO40"
    assert model_for("Something else", maps) is None


async def test_it_identifies_the_pump(plugin: NibePlugin) -> None:
    assert plugin.model is not None
    assert plugin.model.name == "F1245"
    assert plugin.firmware == "9721R4"
    assert plugin.high_word_first is True
    described = plugin.describe()
    unit = described.nodes[0]
    assert unit.identity is not None
    assert (unit.identity.model, unit.identity.firmware) == ("F1245", "9721R4")
    assert {n.path for n in described.nodes} >= {
        "hp1",
        "hp1/cs1",
        "hp1/dhw",
        "hp1/compressor.ep14",
        "hp1/brine",
        "hp1/addition",
    }
    assert "hp1/cs2" not in {n.path for n in described.nodes}


async def test_values_and_their_quality(plugin: NibePlugin, stocked: SimPump) -> None:
    await until(lambda: all_read(plugin))
    assert value(plugin, "outdoor.temp") == (-6.0, "good", None)
    assert value(plugin, "cs1/supply.temp") == (35.0, "good", None)
    assert value(plugin, "degree_minutes") == (-22.4, "good", None)
    assert value(plugin, "demand") == ("heating", "good", None)
    assert value(plugin, "diverter") == ("heating", "good", None)
    assert value(plugin, "compressor.ep14/state") == ("running", "good", None)
    assert value(plugin, "brine/brine.out.temp") == (
        None,
        "not_connected",
        "the pump reports no sensor",
    )
    assert value(plugin, "heat.produced{purpose=dhw,by=total}") == (1234.5, "good", None)
    assert value(plugin, "cs1/x.nibe.47011") == (-4, "good", None)
    assert plugin.envelope("hp1/outdoor.temp").unit == "degC"


async def test_flow_rules(plugin: NibePlugin, stocked: SimPump) -> None:
    stocked.registers[43437] = 0  # supply pump stopped
    await until(lambda: value(plugin, "cs1/supply.temp")[1] == "no_flow")
    assert value(plugin, "cs1/supply.temp")[2] == "pump 43437 = 0"
    stocked.registers[43437] = 50
    stocked.registers[43427] = 40  # compressor starting
    await until(lambda: value(plugin, "cs1/supply.temp")[1] == "transitional")
    assert value(plugin, "brine/brine.in.temp")[1] == "transitional"


async def test_a_hot_water_charge(plugin: NibePlugin, stocked: SimPump) -> None:
    stocked.registers[43086] = 20  # prio: hot water
    await until(lambda: value(plugin, "demand")[0] == "dhw")
    assert value(plugin, "diverter") == ("dhw", "good", None)
    await until(lambda: value(plugin, "dhw/temp.charge")[1] == "transitional")
    assert value(plugin, "cs1/supply.temp") == (35.0, "good", "diverter to dhw")
    stocked.registers[43086] = 10  # idle
    await until(lambda: value(plugin, "demand")[0] == "idle")
    assert value(plugin, "diverter")[:2] == (None, "unknown")
    assert value(plugin, "dhw/temp.charge")[1] == "good"


async def test_a_heat_meter_that_doesnt_count(
    plugin: NibePlugin, stocked: SimPump, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A meter that stays the same through its purpose's production doesn't count on this
    pump: its value is shown as unknown, with why, until it moves."""
    monkeypatch.setattr(profile, "METER_IDLE_S", 1.0)
    meter = "heat.produced{purpose=dhw,by=total}"
    await until(lambda: value(plugin, meter)[1] == "good")
    stocked.registers[43086] = 20  # hot water, with the compressor running
    await until(lambda: value(plugin, meter)[1] == "unknown")
    assert (value(plugin, meter)[2] or "").startswith("hasn't changed in ")
    assert (value(plugin, meter)[2] or "").endswith(" of dhw production")
    stocked.registers32[42437] += 10  # it counts after all
    await until(lambda: value(plugin, meter)[1] == "good")


async def test_the_brines_delta_t_and_its_warning(stocked: SimPump, gateway: Gateway) -> None:
    """The brine's delta-T, worked out; and a warning near the pump's own low brine-out
    limit while the compressor runs, which ends a little above it, said once each way."""
    stocked.registers.update({40015: 30, 40016: -20, 43439: 50, 47381: -80})  # 3.0, -2.0 °C
    events: list[object] = []
    p = NibePlugin(
        settings(gateway),
        transport_settings={"plain_settings": FAST_PLAIN},
        identify_timeout_s=5,
        health_interval_s=0.1,
    )

    def warnings() -> list[DeviceEvent]:
        return [e for e in events if isinstance(e, DeviceEvent) and e.code == "brine.out.low"]

    async def brine_out(raw: int) -> None:
        stocked.registers[40016] = raw
        await until(lambda: value(p, "brine/brine.out.temp")[0] == raw / 10)
        await asyncio.sleep(0.3)  # a few health rounds, where the warning is judged

    async with running_plugin(p, events):
        await until(lambda: value(p, "brine/brine.delta_t")[0] == 5.0)
        assert value(p, "brine/brine.delta_t") == (5.0, "good", None)
        await until(lambda: value(p, "brine/x.nibe.47381")[0] == -8.0)  # the pump's own
        assert "hp1/brine/heat.extracted.power" not in {x.path for x in p.describe().points}
        await brine_out(-20)
        assert warnings() == []  # -2.0 is far from -8.0
        await brine_out(-62)  # -6.2 °C: within 2 °C of the limit
        [warning] = warnings()
        assert warning.active is True
        assert "-6.2 °C" in warning.text
        assert "(-8.0 °C)" in warning.text
        await brine_out(-57)  # -5.7: above the limit + 2, not yet + 2.5
        assert len(warnings()) == 1
        await brine_out(-50)  # -5.0: clear
        assert [w.active for w in warnings()] == [True, False]
        stocked.registers[43439] = 0  # the brine pump stands still
        await until(lambda: value(p, "brine/brine.delta_t")[1] == "no_flow")


async def test_the_heat_taken_from_the_ground(stocked: SimPump, gateway: Gateway) -> None:
    """With a brine flow entered: power from the flow, scaled by the pump's speed, times
    what the brine carries per kelvin, times the delta-T; and it adds up."""
    stocked.registers.update({40015: 30, 40016: -10, 43439: 50})  # delta-T 4.0 K
    p = NibePlugin(
        settings(gateway, brine_flow=40.0, brine_flow_at=100, brine_mix="ethanol28"),
        transport_settings={"plain_settings": FAST_PLAIN},
        identify_timeout_s=5,
        health_interval_s=0.5,
    )
    async with running_plugin(p) as running:
        power = "brine/heat.extracted.power"
        await until(lambda: value(running, power)[0] not in (None, 0.0))
        # 40 L/min at 100 %, so 20 at 50 %: 20/60 L/s * 4.08 kJ/(L K) * 4 K = 5.44 kW.
        assert value(running, power)[0] == pytest.approx(5.44, abs=0.01)
        assert (
            value(running, power)[2] == "estimated from 40 L/min at 100 % and the brine's delta-T"
        )
        assert running.envelope(f"hp1/{power}").source == "estimated"
        # It adds up (shown to 0.1 kWh, which takes a while at 5 kW).
        await until(lambda: running._extracted_kwh > 0)
        assert value(running, "brine/heat.extracted")[1] == "good"
        stocked.registers[43439] = 0
        await until(lambda: value(running, power)[0] == 0.0)


class KeptState:
    """A plugin store that lives as long as the test: what a restart keeps."""

    def __init__(self) -> None:
        self.data: dict[str, Any] = {}
        self.saves = 0

    async def load(self) -> dict[str, Any]:
        return dict(self.data)

    async def save(self, data: dict[str, Any]) -> None:
        self.data = data
        self.saves += 1


async def test_a_meters_count_outlives_a_restart(
    stocked: SimPump, gateway: Gateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The count is kept, and holds after a restart if the meter still reads the same;
    a meter that moved meanwhile starts over."""
    monkeypatch.setattr(profile, "METER_IDLE_S", 1.0)
    monkeypatch.setattr("thermaestro.nibe.plugin.METER_SAVE_S", 0.1)
    kept = KeptState()
    meter = "heat.produced{purpose=dhw,by=total}"

    def made() -> NibePlugin:
        return NibePlugin(
            settings(gateway),
            transport_settings={"plain_settings": FAST_PLAIN},
            identify_timeout_s=5,
            health_interval_s=0.5,
            state=kept,  # type: ignore[arg-type]
        )

    stocked.registers[43086] = 20  # hot water, with the compressor running
    async with running_plugin(made()) as p:
        await until(lambda: value(p, meter)[1] == "unknown")
        await until(lambda: kept.data.get("meters", {}).get("42437", {}).get("idle", 0) >= 1.0)
    stocked.registers[43086] = 10  # idle: no production after the restart
    async with running_plugin(made()) as p:
        await until(lambda: value(p, meter)[2] != "not read yet")
        assert value(p, meter)[1] == "unknown"  # the count held
    stocked.registers32[42437] += 10  # it moved while Thermaestro was down
    async with running_plugin(made()) as p:
        await until(lambda: value(p, meter)[2] != "not read yet")
        assert value(p, meter)[1] == "good"


async def test_it_conforms_and_writes_nothing(stocked: SimPump, gateway: Gateway) -> None:
    p = NibePlugin(
        settings(gateway), transport_settings={"plain_settings": FAST_PLAIN}, identify_timeout_s=5
    )
    assert await run_conformance(p) == []
    assert stocked.taken_writes == []


async def run_conformance(p: NibePlugin) -> list[object]:
    core, plugin_side = pair()
    served = asyncio.create_task(serve(plugin_side, p))
    try:
        return list(await run(core, timeout_s=30, quiet_s=0.3))
    finally:
        await plugin_side.close()
        served.cancel()
        await asyncio.gather(served, return_exceptions=True)


async def test_a_second_climate_system_is_detected(stocked: SimPump, gateway: Gateway) -> None:
    stocked.registers.update({47302: 1, 40007: 250, 47303: 1})  # S3 on, its sensor missing
    p = NibePlugin(
        settings(gateway), transport_settings={"plain_settings": FAST_PLAIN}, identify_timeout_s=5
    )
    async with running_plugin(p) as running:
        nodes = {n.path: n for n in running.describe().nodes}
        assert "hp1/cs2" in nodes
        assert "hp1/cs3" not in nodes
        presence = nodes["hp1/cs2"].presence
        assert (presence.how, presence.rule) == (
            "detected",
            "47302 = 1 and supply sensor 40007 connected",
        )


async def test_registers_the_pump_pushes_arent_polled(stocked: SimPump, gateway: Gateway) -> None:
    stocked.log_set = [40004, 43086]
    stocked.log_set_interval_s = 0.1
    p = NibePlugin(
        settings(gateway), transport_settings={"plain_settings": FAST_PLAIN}, identify_timeout_s=5
    )
    async with running_plugin(p) as running:
        await until(lambda: {40004, 43086} <= running.pushed)
        points = {pt.path: pt for pt in running.describe().points}
        assert points["hp1/outdoor.temp"].delivery.how == "pushed"
        assert points["hp1/cs1/supply.temp"].delivery.how == "polled"
        taken = len(stocked.taken_reads)
        await asyncio.sleep(1.0)
        assert 40004 not in stocked.taken_reads[taken:]


async def test_a_given_model_needs_no_product_information(
    stocked: SimPump, gateway: Gateway
) -> None:
    stocked.info_interval_s = 3600
    p = NibePlugin(
        settings(gateway, model="F1245"),
        transport_settings={"plain_settings": FAST_PLAIN},
        identify_timeout_s=1,
    )
    async with running_plugin(p) as running:
        assert running.model is not None
        assert running.model.name == "F1245"


async def test_an_unknown_product_is_reported(stocked: SimPump, gateway: Gateway) -> None:
    stocked.product = "X9999"
    p = NibePlugin(
        settings(gateway), transport_settings={"plain_settings": FAST_PLAIN}, identify_timeout_s=5
    )
    core, plugin_side = pair()
    served = asyncio.create_task(serve(plugin_side, p))
    try:
        with pytest.raises(NotIdentified, match="X9999"):
            await asyncio.wait_for(asyncio.shield(p._identified), 10)
    finally:
        await plugin_side.close()
        await core.close()
        served.cancel()
        await asyncio.gather(served, return_exceptions=True)


async def test_the_gateway_protocol_with_its_key(
    stocked: SimPump, gateway_with_psk: Gateway, tmp_path: Path
) -> None:
    secrets = SecretStore(tmp_path / "secrets.json")
    await secrets.set("nibe.psk", TEST_PSK.hex())
    p = NibePlugin(
        NibeGateway(
            host="127.0.0.1",
            protocol="thermaestro-gw",
            control_port=gateway_with_psk.ports["control"],
            read_port=gateway_with_psk.ports["read"],
            write_port=gateway_with_psk.ports["write"],
            psk="nibe.psk",
        ),
        secrets=secrets,
        transport_settings={"tgw_settings": FAST_TGW},
        identify_timeout_s=5,
    )
    async with running_plugin(p) as running:
        transport = running.describe().nodes[0].transport
        assert transport is not None
        assert transport.fate == "exact"
        await until(lambda: value(running, "outdoor.temp")[1] == "good")


def test_the_entry_point_is_registered() -> None:
    assert "nibe" in discover()


def test_log_set() -> None:
    model = load("bus").model("F1245")
    data = logset.render(model, [40004, 43005], day=date(2026, 10, 6))
    lines = data.decode("latin-1").split("\r\n")
    assert lines[0] == "[NIBL;20261006;9696]"
    assert lines[1] == "Divisors\t\t10\t10"
    assert lines[2] == "Date\tTime\tBT1 Outdoor Temperature [°C]\tDegree Minutes (16 bit)"
    assert lines[3:] == ["40004", "43005"]
    assert not data.endswith(b"\r\n")
    with pytest.raises(ValueError, match="1 to 20"):
        logset.render(model, list(range(40004, 40030)), day=date(2026, 10, 6))
    assert all(r in model for r in profile.LOG_SET)


async def test_points_carry_labels_and_counter_sizes(plugin: NibePlugin) -> None:
    points = {p.path: p for p in plugin.describe().points}
    assert points["hp1/x.nibe.47375"].label == "Stop Temperature Heating"
    assert points["hp1/outdoor.temp"].label is None  # the standard name says it
    meter = points["hp1/heat.produced{purpose=dhw,by=total}"]
    assert meter.wraps_at == (1 << 32) / 10
    assert points["hp1/outdoor.temp"].wraps_at is None


async def test_a_meter_the_pump_doesnt_keep_is_left_out(stocked: SimPump, gateway: Gateway) -> None:
    p = NibePlugin(
        settings(gateway),
        transport_settings={"plain_settings": FAST_PLAIN},
        identify_timeout_s=5,
        health_interval_s=0.5,
    )
    removed: list[str] = []

    def heard(message: object) -> None:
        if isinstance(message, Described):
            removed.extend(message.removed)

    meter = "hp1/heat.produced{purpose=heating,by=total}"
    core, plugin_side = pair()
    served = asyncio.create_task(serve(plugin_side, p))
    link = Link(core, on_event=heard)
    link.start()
    try:
        await link.hello(timeout=5)
        first = await link.describe(timeout=15)
        assert meter in {pt.path for pt in first.points}  # described at once
        await until(lambda: meter in removed)
        assert "hp1/heat.produced{purpose=dhw,by=total}" not in removed
        again = await link.describe(timeout=5)
        assert meter not in {pt.path for pt in again.points}
        assert 42439 in p.absent
    finally:
        await link.close()
        await plugin_side.close()
        served.cancel()
        await asyncio.gather(served, return_exceptions=True)


def test_what_register_values_mean() -> None:
    from thermaestro.nibe.plugin import is_switch, value_texts

    m = load("bus").model("F1245")
    assert value_texts(m.register(47137)) == {0: "Auto", 1: "Manual", 2: "Add. heat only"}
    assert value_texts(m.register(47041)) == {
        0: "Economy",
        1: "Normal",
        2: "Luxury",
        4: "Smart Control",
    }
    assert value_texts(m.register(48132)) == {
        0: "Off",
        1: "3h",
        2: "6h",
        3: "12h",
        4: "One time increase",
    }
    assert value_texts(m.register(40004)) is None
    assert value_texts(m.register(43005)) is None
    assert is_switch(m.register(47370))  # allow additive heating: 0 or 1, nothing said
    assert is_switch(m.register(47387))  # 0=Off 1=On
    assert not is_switch(m.register(47137))


async def test_settings_show_their_meaning(plugin: NibePlugin, stocked: SimPump) -> None:
    stocked.registers.update({47137: 1, 47370: 1, 47041: 2})
    await until(lambda: all_read(plugin))
    await until(lambda: value(plugin, "x.nibe.47137")[0] == "Manual")
    assert value(plugin, "x.nibe.47370") == (True, "good", None)
    assert value(plugin, "dhw/x.nibe.47041") == ("Luxury", "good", None)
    points = {p.path: p for p in plugin.describe().points}
    assert points["hp1/dhw/x.nibe.47041"].label == "Hot water comfort mode"
    assert points["hp1/outdoor.temp"].description == "Current outdoor temperature"
    assert points["hp1/outdoor.temp"].category is None  # every day
    assert points["hp1/x.nibe.47137"].category == "config"  # a setting
    assert points["hp1/x.nibe.48852"].category == "diagnostic"  # the word order
    assert points["hp1/x.nibe.47137"].enum.value == {"Auto": 0, "Manual": 1, "Add. heat only": 2}
