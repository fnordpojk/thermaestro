"""The Nibe plugin's write side, against the simulated pump through the Python gateway:
every lever over both routes, the hot-water block and what it keeps, another client's
writes, and the executor driving it all from shadow to control and back."""

import asyncio
import contextlib
import socket
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from simpump import TEST_PSK, OtherClient, SimPump
from test_nibe_plugin import FAST_PLAIN, FAST_TGW, PUMP, settings, until
from thermaestro_gateway.server import Config, Gateway

from thermaestro.cap import Link, pair, serve
from thermaestro.cap.messages import Described, Fate, ForeignWrite, Op
from thermaestro.cap.model import Value
from thermaestro.core import Core, Key, run
from thermaestro.core.plugins import PluginStore
from thermaestro.nibe import profile
from thermaestro.nibe.maps import load
from thermaestro.nibe.plugin import NibePlugin
from thermaestro.store import Control, Database, Layout, NibeGateway, Plugin, SecretStore

SCHEDULE = "the pump's hot-water schedule (menu 2.3)"

STOCK = {
    **PUMP,
    47394: 0,  # the pump's room control: off
    47043: 480,  # start temperatures: Luxury 48.0, Normal 45.0, Economy 42.0
    47044: 450,
    47045: 420,
    47137: 0,  # auto mode
    47375: 170,  # heating stop 17.0
    47376: 50,  # addition stop 5.0
    47212: 650,  # most internal addition power 6.5 kW
    48088: 1,  # pool 1: accessory on, BT51 25.0, start 22.0, stop 28.0, activated
    40042: 250,
    48090: 220,
    48092: 280,
    48094: 1,
    48087: 0,  # no pool 2
}


@pytest.fixture
def stocked(pump: SimPump) -> SimPump:
    pump.registers.update(STOCK)
    pump.info_interval_s = 0.2
    return pump


def plugin(gateway: Gateway, state: PluginStore | None = None) -> NibePlugin:
    return NibePlugin(
        settings(gateway),
        transport_settings={"plain_settings": FAST_PLAIN},
        identify_timeout_s=5,
        health_interval_s=0.5,
        state=state,
    )


@contextlib.asynccontextmanager
async def served(p: NibePlugin, events: list[object] | None = None) -> AsyncIterator[Link]:
    """The plugin served as the core would, described; the core's end of the link."""
    core, plugin_side = pair()
    task = asyncio.create_task(serve(plugin_side, p))
    link = Link(core, on_event=events.append if events is not None else None)
    link.start()
    try:
        await link.hello(timeout=5)
        await link.describe(timeout=15)
        yield link
    finally:
        await link.close()
        await plugin_side.close()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def act(link: Link, lever: str, op: Op, params: dict[str, Value] | None = None) -> Fate:
    """The final fate of an act."""
    fates = [f async for f in link.act(f"hp1/{lever}", op, params or {}, timeout=20)]
    return fates[-1]


def signed(word: int) -> int:
    word &= 0xFFFF
    return word - 0x1_0000 if word & 0x8000 else word


async def test_the_levers_on_offer(stocked: SimPump, gateway: Gateway) -> None:
    async with served(plugin(gateway)) as link:
        described = await link.describe(timeout=5)
    levers = {lv.path.removeprefix("hp1/"): lv for lv in described.levers}
    assert set(levers) == {
        *profile.LEVERS,
        "pool1/start_temp",
        "pool1/stop_temp",
        "pool1/block",
    }
    offset = levers["cs1/heating.offset"]
    assert offset.preconditions.value == ("hp1/cs1/x.nibe.47394 == 0",)
    assert offset.competing_features == ()
    mode = levers["dhw/mode"]
    assert mode.params["value"].enum.value == {"eco": 0, "normal": 1, "lux": 2}
    assert [f.name for f in mode.competing_features] == [SCHEDULE]
    block = levers["dhw/block"]
    assert block.implementation.how == "Normal's start temperature (47044) lowered to 25.0 °C"
    assert [f.name for f in block.competing_features] == [SCHEDULE]
    assert levers["addition/stop_temp"].preconditions.value == ("hp1/x.nibe.47137 == 0",)
    assert levers["pool1/block"].kind == "hold"
    nodes = {n.path: n for n in described.nodes}
    assert nodes["hp1/pool1"].kind == "pool"
    assert nodes["hp1/pool1"].presence.rule == "48088 = 1 and pool sensor 40042 connected"
    assert "hp1/pool2" not in nodes
    points = {p.path: p for p in described.points}
    assert points["hp1/pool1/temp"].unit == "degC"


def test_no_lever_writes_what_is_never_written() -> None:
    maps = load("bus")
    for name in maps.models:
        model = maps.model(name)
        layout = profile.BUS.layout(model, list(range(1, 9)), [1, 2])
        for spec in profile.levers(model, layout.points, {}):
            registers = {int(t.removeprefix("x.nibe.")) for t in spec.lever.touches}
            assert not registers & profile.NEVER_WRITTEN, (name, spec.path)
            assert all(model.register(r).writable for r in registers), (name, spec.path)


async def over_the_protocol(gateway: Gateway, tmp_path: Path) -> NibePlugin:
    """A plugin on the Thermaestro gateway protocol, with the gateway's key."""
    secrets = SecretStore(tmp_path / "secrets.json")
    await secrets.set("nibe.psk", TEST_PSK.hex())
    return NibePlugin(
        NibeGateway(
            host="127.0.0.1",
            protocol="thermaestro-gw",
            control_port=gateway.ports["control"],
            read_port=gateway.ports["read"],
            write_port=gateway.ports["write"],
            psk="nibe.psk",
        ),
        secrets=secrets,
        transport_settings={"tgw_settings": FAST_TGW},
        identify_timeout_s=5,
    )


@pytest.fixture(params=["nibegw", "thermaestro-gw"])
async def routed(
    request: pytest.FixtureRequest, stocked: SimPump, tmp_path: Path
) -> AsyncIterator[NibePlugin]:
    """A plugin over plain NibeGW, and one over the Thermaestro gateway protocol."""
    protocol = request.param == "thermaestro-gw"
    gateway = Gateway(
        Config(
            serial_port=stocked.end.path,
            listen="127.0.0.1",
            read_port=0,
            write_port=0,
            control_port=0,
            psk=TEST_PSK if protocol else None,
        )
    )
    await gateway.start()
    stocked.start()
    try:
        yield await over_the_protocol(gateway, tmp_path) if protocol else plugin(gateway)
    finally:
        await stocked.stop()
        await gateway.close()


@pytest.mark.parametrize(
    ("lever", "op", "params", "register", "word"),
    [
        ("cs1/heating.offset", "set", {"value": 2}, 47011, 2),
        ("cs1/heating.offset", "set", {"value": -7}, 47011, -7),
        ("dhw/mode", "set", {"value": "eco"}, 47041, 0),
        ("dhw/block", "engage", {}, 47044, 250),
        ("dhw/boost_once", "fire", {}, 48132, 4),
        ("dhw/boost_once", "cancel", {}, 48132, 0),
        ("alarm.reset", "fire", {}, 45171, 1),
        ("addition/stop_temp", "set", {"value": -10.5}, 47376, -105),
        ("addition/max_power", "set", {"value": 3.5}, 47212, 350),
        ("pool1/start_temp", "set", {"value": 21.5}, 48090, 215),
        ("pool1/stop_temp", "set", {"value": 29}, 48092, 290),
        ("pool1/block", "engage", {}, 48094, 0),
    ],
)
async def test_each_lever_over_each_route(
    stocked: SimPump,
    routed: NibePlugin,
    lever: str,
    op: Op,
    params: dict[str, Value],
    register: int,
    word: int,
) -> None:
    async with served(routed) as link:
        fate = await act(link, lever, op, params)
    assert fate.stage == "device_accepted", fate.detail
    assert [r for r, _ in stocked.taken_writes] == [register]
    assert signed(stocked.registers[register]) == word


async def test_over_the_gateway_protocol(
    stocked: SimPump, gateway_with_psk: Gateway, tmp_path: Path
) -> None:
    p = await over_the_protocol(gateway_with_psk, tmp_path)
    events: list[object] = []
    async with served(p, events) as link:
        assert (await act(link, "cs1/heating.offset", "set", {"value": 3})).stage == (
            "device_accepted"
        )
        assert (await act(link, "dhw/block", "engage")).stage == "device_accepted"
        assert stocked.registers[47044] == 250
        assert (await act(link, "dhw/block", "release")).stage == "device_accepted"
        stocked.refuse.add(47041)
        assert (await act(link, "dhw/mode", "set", {"value": "lux"})).stage == "device_refused"
        await asyncio.sleep(0.5)
    assert signed(stocked.registers[47011]) == 3
    assert stocked.registers[47044] == 450
    assert not [e for e in events if isinstance(e, ForeignWrite)]  # its own aren't reported


async def test_what_isnt_sent(stocked: SimPump, gateway: Gateway) -> None:
    async with served(plugin(gateway)) as link:
        cases: list[tuple[str, Op, dict[str, Value], str]] = [
            ("cs1/heating.offset", "set", {"value": 11}, "outside the register's range"),
            ("cs1/heating.offset", "set", {"value": 1.5}, "isn't a step"),
            ("cs1/heating.offset", "set", {"value": True}, "isn't a number"),
            ("dhw/mode", "set", {"value": "turbo"}, "isn't one of"),
            ("dhw/mode", "set", {"value": 3}, "isn't one of"),
            ("dhw/boost_once", "set", {"value": 1}, "doesn't take set"),
            ("alarm.reset", "cancel", {}, "doesn't take cancel"),
            ("dhw/block", "release", {}, "nothing engaged by this plugin"),
            ("cs9/heating.offset", "set", {"value": 1}, "no such lever"),
        ]
        for lever, op, params, why in cases:
            fate = await act(link, lever, op, params)
            assert fate.stage == "dropped", lever
            assert why in (fate.detail or ""), (lever, fate.detail)
    assert stocked.taken_writes == []


async def test_a_refused_write_and_one_not_kept(stocked: SimPump, gateway: Gateway) -> None:
    stocked.refuse.add(47011)
    stocked.not_kept.add(47041)
    async with served(plugin(gateway)) as link:
        refused = await act(link, "cs1/heating.offset", "set", {"value": 1})
        dropped = await act(link, "dhw/mode", "set", {"value": "eco"})
    assert (refused.stage, refused.detail) == ("device_refused", "0x6C = 0")
    assert dropped.stage == "device_accepted"  # accepted isn't kept: the core reads it back
    assert stocked.registers[47041] == 1


async def test_the_hot_water_block(stocked: SimPump, gateway: Gateway) -> None:
    p = plugin(gateway)
    async with served(p) as link:
        assert (await act(link, "dhw/block", "engage")).stage == "device_accepted"
        assert stocked.registers[47044] == 250
        assert p.holds == {"hp1/dhw/block": {"register": 47044, "value": 45.0, "mode": 1}}
        assert (await act(link, "dhw/block", "release")).stage == "device_accepted"
        assert stocked.registers[47044] == 450
        assert p.holds == {}
        # A start already as low as the block's: what to put back isn't known.
        stocked.registers[47045] = 250
        stocked.registers[47041] = 0
        fate = await act(link, "dhw/block", "engage")
        assert fate.stage == "dropped"
        assert "already reads 25 °C" in (fate.detail or "")
        # Smart Control has no start of its own.
        stocked.registers[47041] = 4
        fate = await act(link, "dhw/block", "engage")
        assert (fate.stage, fate.detail) == (
            "dropped",
            "Smart Control has no start temperature of its own to lower",
        )
    assert [r for r, _ in stocked.taken_writes] == [47044, 47044]


async def test_the_pool_block(stocked: SimPump, gateway: Gateway) -> None:
    p = plugin(gateway)
    async with served(p) as link:
        assert (await act(link, "pool1/block", "engage")).stage == "device_accepted"
        assert stocked.registers[48094] == 0
        assert (await act(link, "pool1/block", "release")).stage == "device_accepted"
    assert stocked.registers[48094] == 1


async def test_a_blocks_release_outlives_a_restart(
    stocked: SimPump, gateway: Gateway, tmp_path: Path
) -> None:
    async with await Database.open(tmp_path / "db.sqlite") as db:
        state = PluginStore(db, "pump")
        async with served(plugin(gateway, state)) as link:
            assert (await act(link, "dhw/block", "engage")).stage == "device_accepted"
        assert stocked.registers[47044] == 250
        assert (await state.load())["holds"] == {
            "hp1/dhw/block": {"register": 47044, "value": 45.0, "mode": 1}
        }
        again = plugin(gateway, state)
        async with served(again) as link:
            assert (await act(link, "dhw/block", "release")).stage == "device_accepted"
        assert stocked.registers[47044] == 450
        assert (await state.load())["holds"] == {}


async def test_the_block_moves_with_the_mode(stocked: SimPump, gateway: Gateway) -> None:
    """Engaged on Normal, the mode goes to Economy: release puts Normal's start back,
    engage lowers Economy's."""
    events: list[object] = []
    async with served(plugin(gateway), events) as link:
        await act(link, "dhw/block", "engage")
        stocked.registers[47041] = 0
        await until(lambda: any(isinstance(e, Described) for e in events))
        anew = next(e for e in events if isinstance(e, Described))
        assert not anew.complete
        assert [lv.implementation.how for lv in anew.levers] == [
            "Economy's start temperature (47045) lowered to 25.0 °C"
        ]
        await act(link, "dhw/block", "release")
        await act(link, "dhw/block", "engage")
    assert (stocked.registers[47044], stocked.registers[47045]) == (450, 250)


async def test_an_engage_puts_a_stray_block_back_first(stocked: SimPump, gateway: Gateway) -> None:
    """A block left engaged on another mode's start is put back before the next one."""
    p = plugin(gateway)
    async with served(p) as link:
        await act(link, "dhw/block", "engage")
        stocked.registers[47041] = 0
        assert (await act(link, "dhw/block", "engage")).stage == "device_accepted"
    assert (stocked.registers[47044], stocked.registers[47045]) == (450, 250)
    assert p.holds == {"hp1/dhw/block": {"register": 47045, "value": 42.0, "mode": 0}}


async def test_another_clients_write_is_reported(stocked: SimPump, gateway: Gateway) -> None:
    events: list[object] = []
    other = OtherClient("127.0.0.1", gateway.ports["read"], gateway.ports["write"])
    await other.start()
    try:
        async with served(plugin(gateway), events) as link:
            await act(link, "cs1/heating.offset", "set", {"value": 1})
            other.write(47011, 0xFFFF_FFFE)
            await until(lambda: any(isinstance(e, ForeignWrite) for e in events))
            await asyncio.sleep(0.3)
    finally:
        other.close()
    foreign = [e for e in events if isinstance(e, ForeignWrite)]
    assert [(e.unit, e.datapoint, e.value) for e in foreign] == [("hp1", "x.nibe.47011", -2)]


async def test_nothing_is_written_unasked(stocked: SimPump, gateway: Gateway) -> None:
    async with served(plugin(gateway)):
        stocked.registers[47041] = 0  # what the block follows changes
        await asyncio.sleep(2.0)
    assert stocked.taken_writes == []


# --- With the executor --------------------------------------------------------------------


READ = (
    "cs1/x.nibe.47011",
    "cs1/x.nibe.47394",
    "dhw/x.nibe.47041",
    "x.nibe.47137",
    "addition/x.nibe.47376",
)


def good(core: Core, point: str) -> bool:
    found = core.values.latest.get(Key("pump", f"hp1/{point}"))
    return found is not None and found.quality == "good"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port: int = s.getsockname()[1]
        return port


async def daemon(
    tmp_path: Path,
    gateway: Gateway,
    levers: dict[str, Any],
    started: Any,
) -> None:
    lay = Layout(tmp_path / "config", tmp_path / "state")
    lay.config.mkdir()
    lay.startup.write_text(
        f'[web]\nlisten = "127.0.0.1"\nport = {free_port()}\nhttps_port = {free_port()}\n'
    )
    lay.state.mkdir(mode=0o700)
    async with await Database.open(lay.database) as db:
        gw = settings(gateway)
        await db.put(
            Plugin(
                plugin="nibe",
                settings={"host": gw.host, "read_port": gw.read_port, "write_port": gw.write_port},
            ),
            "pump",
        )
        confirmed = {f"pump:hp1/{lv}": (SCHEDULE,) for lv in ("dhw/mode", "dhw/block")}
        await db.put(
            Control(levers={f"pump:hp1/{k}": v for k, v in levers.items()}, confirmed_off=confirmed)
        )

    def factory(context: Any) -> NibePlugin:
        return NibePlugin(
            NibeGateway.model_validate(dict(context.settings)),
            state=context.state,
            transport_settings={"plain_settings": FAST_PLAIN},
            identify_timeout_s=5,
            health_interval_s=0.5,
        )

    stop = asyncio.Event()

    async def then(core: Core) -> None:
        try:
            async with asyncio.timeout(20):  # the points the levers read back and rest on
                while not all(good(core, p) for p in READ):
                    await asyncio.sleep(0.05)
            await started(core)
        finally:
            stop.set()

    await asyncio.wait_for(
        run(lay, stop=stop, factories={"nibe": factory}, started=then, flush_s=0.2), 120
    )


async def test_the_executor_in_control_and_putting_back(
    stocked: SimPump, gateway: Gateway, tmp_path: Path
) -> None:
    outcomes: dict[str, str] = {}

    async def started(core: Core) -> None:
        requests: list[tuple[str, str, Op, dict[str, Value]]] = [
            ("offset", "cs1/heating.offset", "set", {"value": 2}),
            ("block", "dhw/block", "engage", {}),
            ("smart", "dhw/mode", "set", {"value": "smart"}),
            ("eco", "dhw/mode", "set", {"value": "eco"}),
            ("reset", "alarm.reset", "fire", {}),
        ]
        for name, lever, op, params in requests:
            result = await core.executor.act(f"pump:hp1/{lever}", op, params, who="planner")
            outcomes[name] = f"{result.outcome}: {result.detail}"
        # The mode changed under the block: it moves to Economy's start.
        async with asyncio.timeout(20):
            while stocked.registers[47045] != 250 or stocked.registers[47044] != 450:
                await asyncio.sleep(0.1)

    await daemon(
        tmp_path,
        gateway,
        {
            "cs1/heating.offset": "control",
            "dhw/block": "control",
            "dhw/mode": "control",
            "alarm.reset": "control",
        },
        started,
    )
    assert outcomes["offset"] == "verified: None"
    assert outcomes["block"].startswith("awaiting_effect")
    assert outcomes["smart"] == "refused: 'smart' isn't one of eco, normal, lux"
    assert outcomes["eco"] == "verified: None"
    assert outcomes["reset"] == "refused: only a person may use this lever"
    # Stopping put everything back as it was found.
    assert signed(stocked.registers[47011]) == -4
    assert stocked.registers[47041] == 1
    assert (stocked.registers[47044], stocked.registers[47045]) == (450, 420)
    assert 45171 not in [r for r, _ in stocked.taken_writes]


async def test_the_room_control_must_be_off(
    stocked: SimPump, gateway: Gateway, tmp_path: Path
) -> None:
    stocked.registers[47394] = 1
    outcomes: list[str] = []

    async def started(core: Core) -> None:
        result = await core.executor.act(
            "pump:hp1/cs1/heating.offset", "set", {"value": 1}, who="planner"
        )
        outcomes.append(f"{result.outcome}: {result.detail}")

    await daemon(tmp_path, gateway, {"cs1/heating.offset": "control"}, started)
    assert outcomes == ["refused: hp1/cs1/x.nibe.47394 == 0 doesn't hold"]
    assert stocked.taken_writes == []


async def test_shadow_sends_nothing(stocked: SimPump, gateway: Gateway, tmp_path: Path) -> None:
    outcomes: list[str] = []

    async def started(core: Core) -> None:
        requests: list[tuple[str, Op, dict[str, Value]]] = [
            ("cs1/heating.offset", "set", {"value": 2}),
            ("dhw/block", "engage", {}),
            ("dhw/mode", "set", {"value": "eco"}),
            ("addition/stop_temp", "set", {"value": -20}),
            ("pool1/block", "engage", {}),
        ]
        for lever, op, params in requests:
            result = await core.executor.act(f"pump:hp1/{lever}", op, params, who="planner")
            outcomes.append(result.outcome)

    shadow = ("cs1/heating.offset", "dhw/block", "dhw/mode", "addition/stop_temp", "pool1/block")
    await daemon(tmp_path, gateway, dict.fromkeys(shadow, "shadow"), started)
    assert outcomes == ["shadowed"] * 5
    assert stocked.taken_writes == []
