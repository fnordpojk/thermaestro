"""The Nibe plugin's write side, against the simulated pump through the Python gateway:
every lever over both routes, the hot-water block and what it keeps, another client's
writes, and the executor driving it all from shadow to control and back."""

import asyncio
import contextlib
import dataclasses
import socket
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from simpump import TEST_PSK, OtherClient, SimPump
from test_nibe_plugin import FAST_PLAIN, FAST_TGW, PUMP, settings, until
from thermaestro_gateway.protocol import Arm, Armed, ArmRefusal, ArmResult, Request
from thermaestro_gateway.server import Config, Gateway, now_us

from thermaestro import clock
from thermaestro.cap import Link, Message, Send, pair, serve
from thermaestro.cap.messages import DeadmanFired, Described, Fate, ForeignWrite, Op
from thermaestro.cap.model import Knowledge, Value
from thermaestro.core import Core, Key, run
from thermaestro.core.plugins import PluginStore
from thermaestro.nibe import plugin as plugin_module
from thermaestro.nibe import profile
from thermaestro.nibe.maps import decode, load
from thermaestro.nibe.maps.codec import encode
from thermaestro.nibe.plugin import NibePlugin
from thermaestro.nibe.transport.base import WriteOutcome, WriteResult
from thermaestro.nibe.transport.tgw import TgwClient
from thermaestro.store import Control, Database, Layout, NibeGateway, Plugin, SecretStore

SCHEDULE = "the pump's hot-water schedule (menu 2.3)"
PRICED = "the pump's Smart Price Adaption (with myUplink)"

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
    }  # a pool has no block: switched off and left so, it would stay off
    offset = levers["cs1/heating.offset"]
    assert offset.preconditions.value == ("hp1/cs1/x.nibe.47394 == 0",)
    assert [f.name for f in offset.competing_features] == [PRICED]
    mode = levers["dhw/mode"]
    assert mode.params["value"].enum.value == {"eco": 0, "normal": 1, "lux": 2}
    assert [f.name for f in mode.competing_features] == [SCHEDULE, PRICED]
    block = levers["dhw/block"]
    assert block.implementation.how == "Normal's start temperature (47044) lowered to 35.0 °C"
    assert [f.name for f in block.competing_features] == [SCHEDULE, PRICED]
    assert levers["addition/stop_temp"].competing_features == ()
    assert levers["addition/stop_temp"].preconditions.value == ("hp1/x.nibe.47137 == 0",)
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
        for spec in profile.levers(model, layout.points, {}, None):
            registers = {int(t.removeprefix("x.nibe.")) for t in spec.lever.touches}
            assert not registers & profile.NEVER_WRITTEN, (name, spec.path)
            assert all(model.register(r).writable for r in registers), (name, spec.path)


async def over_the_protocol(
    gateway: Gateway, tmp_path: Path, state: PluginStore | None = None
) -> NibePlugin:
    """A plugin on the Thermaestro gateway protocol, with the gateway's key."""
    secrets = SecretStore(tmp_path / "secrets.json")
    await secrets.set("nibe.psk", TEST_PSK.hex())
    return NibePlugin(
        NibeGateway(
            host="127.0.0.1",
            protocol="thermaestro-gw",
            control_port=gateway.ports["control"],
            read_port=gateway.ports["read"],
            psk="nibe.psk",
        ),
        secrets=secrets,
        transport_settings={"tgw_settings": FAST_TGW},
        identify_timeout_s=5,
        state=state,
    )


def test_every_lever_fits_its_register_on_every_model() -> None:
    """The model-table check: each number a lever takes fits its register's range and
    size, and both ends encode and decode back to themselves."""
    maps = load("bus")
    checked = 0
    for name in maps.models:
        model = maps.model(name)
        layout = profile.BUS.layout(model, list(range(1, 9)), [1, 2])
        for spec in profile.levers(model, layout.points, {}, None):
            param = spec.lever.params.get("value")
            if param is None or param.type != "number" or spec.register is None:
                continue
            limits = param.range.value
            assert limits is not None, (name, spec.path)
            register = model.register(spec.register)
            for end in (limits.min, limits.max):
                assert end is not None
                raw = encode(register, end)
                low, high = raw & 0xFFFF, raw >> 16
                decoded = decode(register, low, high, high_word_first=True)
                assert decoded.value == pytest.approx(end), (name, spec.path, end)
                checked += 1
    assert checked > 100


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
        ("dhw/block", "engage", {}, 47044, 350),
        ("dhw/boost_once", "fire", {}, 48132, 4),
        ("dhw/boost_once", "cancel", {}, 48132, 0),
        ("alarm.reset", "fire", {}, 45171, 1),
        ("addition/stop_temp", "set", {"value": -10.5}, 47376, -105),
        ("addition/max_power", "set", {"value": 3.5}, 47212, 350),
        ("pool1/start_temp", "set", {"value": 21.5}, 48090, 215),
        ("pool1/stop_temp", "set", {"value": 29}, 48092, 290),
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
        assert stocked.registers[47044] == 350
        assert (await act(link, "dhw/block", "release")).stage == "device_accepted"
        stocked.refuse.add(47041)
        assert (await act(link, "dhw/mode", "set", {"value": "lux"})).stage == "device_refused"
        await asyncio.sleep(0.5)
    assert signed(stocked.registers[47011]) == 3
    assert stocked.registers[47044] == 450
    assert not [e for e in events if isinstance(e, ForeignWrite)]  # its own aren't reported


async def act_from(
    link: Link, lever: str, op: Op, params: dict[str, Value], baseline: Value
) -> Fate:
    """The final fate of an act that gives the setting as found, as the executor does."""
    fates = [f async for f in link.act(f"hp1/{lever}", op, params, timeout=20, baseline=baseline)]
    return fates[-1]


def armed(gateway: Gateway) -> list[tuple[int, int, int]]:
    """What the gateway's dead-man holds armed: each register, the word it expects there,
    and the value its write puts back."""
    held = gateway.control.deadman
    assert held is not None
    return sorted(
        (e.register, e.expected, int.from_bytes(e.frame[5:9], "little"))
        for e in held.armed.values()
    )


async def test_each_change_arms_the_gateways_dead_man(
    stocked: SimPump, gateway_with_psk: Gateway, tmp_path: Path
) -> None:
    """Over the protocol with a key, a setting changed and a hold engaged are armed, to be
    put back as found; put back by Thermaestro itself, they are disarmed."""
    p = await over_the_protocol(gateway_with_psk, tmp_path)
    async with served(p) as link:
        described = await link.describe(timeout=5)
        unit = next(n for n in described.nodes if n.path == "hp1")
        assert unit.transport is not None
        assert unit.transport.deadman == "supported"
        offset = await act_from(link, "cs1/heating.offset", "set", {"value": 3}, -4)
        assert offset.stage == "device_accepted"
        assert (await act(link, "dhw/block", "engage")).stage == "device_accepted"
        assert armed(gateway_with_psk) == [(47011, 3, 0xFFFF_FFFC), (47044, 350, 450)]
        back = await act_from(link, "cs1/heating.offset", "set", {"value": -4}, -4)
        assert back.stage == "device_accepted"
        assert (await act(link, "dhw/block", "release")).stage == "device_accepted"
        await until(lambda: not armed(gateway_with_psk))
    assert p.armed == {}


async def test_a_change_the_pump_refused_leaves_the_dead_man_as_it_was(
    stocked: SimPump, gateway_with_psk: Gateway, tmp_path: Path
) -> None:
    p = await over_the_protocol(gateway_with_psk, tmp_path)
    async with served(p) as link:
        assert (await act_from(link, "cs1/heating.offset", "set", {"value": 3}, -4)).stage == (
            "device_accepted"
        )
        stocked.refuse.add(47011)
        refused = await act_from(link, "cs1/heating.offset", "set", {"value": 5}, -4)
        assert refused.stage == "device_refused"
        assert armed(gateway_with_psk) == [(47011, 3, 0xFFFF_FFFC)]


@pytest.mark.parametrize(
    ("meanwhile", "outcome", "found"), [(None, "put_back", None), (1, "skipped", 1)]
)
async def test_the_gateway_puts_back_what_a_silent_thermaestro_left(
    stocked: SimPump,
    gateway_with_psk: Gateway,
    tmp_path: Path,
    meanwhile: int | None,
    outcome: str,
    found: int | None,
) -> None:
    """Thermaestro changes the offset and goes silent: the gateway puts it back as found,
    or leaves a value someone set meanwhile. Started again, the plugin tells the core what
    the gateway did, and the gateway forgets its report."""
    held = gateway_with_psk.control.deadman
    assert held is not None
    async with await Database.open(tmp_path / "db.sqlite") as db:
        state = PluginStore(db, "pump")
        p = await over_the_protocol(gateway_with_psk, tmp_path, state)
        async with served(p) as link:
            assert (await act_from(link, "cs1/heating.offset", "set", {"value": 3}, -4)).stage == (
                "device_accepted"
            )
        if meanwhile is not None:
            stocked.registers[47011] = meanwhile  # the pump's menu
        held.silence_us = 200_000  # the silence, shortened
        await until(lambda: bool(held.fired), 10)
        assert signed(stocked.registers[47011]) == (-4 if meanwhile is None else meanwhile)
        events: list[object] = []
        again = await over_the_protocol(gateway_with_psk, tmp_path, state)
        async with served(again, events) as link:
            await until(lambda: any(isinstance(e, DeadmanFired) for e in events), 10)
            await until(lambda: not held.fired)
        told = next(e for e in events if isinstance(e, DeadmanFired))
        assert (told.lever, told.outcome, told.found) == ("hp1/cs1/heating.offset", outcome, found)
        assert told.fired is not None
        assert again.armed == {}


async def test_a_negative_byte_is_armed_as_the_pump_gives_it(
    stocked: SimPump, gateway_with_psk: Gateway, tmp_path: Path
) -> None:
    """A pump may give -2 in an 8-bit register as 0x00FE rather than 0xFFFE: the gateway
    compares words, so the dead-man is armed again with the pump's own."""
    p = await over_the_protocol(gateway_with_psk, tmp_path)
    async with served(p) as link:
        assert (await act_from(link, "cs1/heating.offset", "set", {"value": -2}, -4)).stage == (
            "device_accepted"
        )
        assert armed(gateway_with_psk) == [(47011, 0xFFFE, 0xFFFF_FFFC)]
        stocked.registers[47011] = 0x00FE
        after = datetime.now(UTC)
        await link.read(["hp1/cs1/x.nibe.47011"], after=after, timeout=10)
        await until(lambda: armed(gateway_with_psk) == [(47011, 0x00FE, 0xFFFF_FFFC)])


async def test_a_change_cut_off_before_its_write_is_still_put_back(
    stocked: SimPump, gateway_with_psk: Gateway, tmp_path: Path
) -> None:
    """Changing a value Thermaestro set, the entry for it stays armed beside the new one
    until the write is known to be taken. Cut off between the two, the gateway puts the
    setting back as found from whichever it holds, and the core is told just that: the
    other entry, finding Thermaestro's own earlier value, says nothing new."""
    held = gateway_with_psk.control.deadman
    assert held is not None
    async with await Database.open(tmp_path / "db.sqlite") as db:
        state = PluginStore(db, "pump")
        p = await over_the_protocol(gateway_with_psk, tmp_path, state)
        async with served(p) as link:
            for value in (3, 5):
                fate = await act_from(link, "cs1/heating.offset", "set", {"value": value}, -4)
                assert fate.stage == "device_accepted"
            assert armed(gateway_with_psk) == [(47011, 5, 0xFFFF_FFFC)]
            reached = asyncio.Event()

            async def cut_off(register: int, raw: int) -> WriteOutcome:
                reached.set()
                await asyncio.Event().wait()  # Thermaestro stops here
                raise AssertionError

            p._send_write = cut_off  # type: ignore[method-assign]
            changing = asyncio.create_task(
                act_from(link, "cs1/heating.offset", "set", {"value": 7}, -4)
            )
            await asyncio.wait_for(reached.wait(), 10)
            # The new entry is the lower: it fires first and finds 5, not its 7.
            assert sorted(held.armed) == [0, 1]
            assert armed(gateway_with_psk) == [(47011, 5, 0xFFFF_FFFC), (47011, 7, 0xFFFF_FFFC)]
            changing.cancel()
            await asyncio.gather(changing, return_exceptions=True)
        assert signed(stocked.registers[47011]) == 5
        held.silence_us = 200_000
        await until(lambda: len(held.fired) == 2, 10)
        assert signed(stocked.registers[47011]) == -4
        events: list[object] = []
        again = await over_the_protocol(gateway_with_psk, tmp_path, state)
        async with served(again, events) as link:
            await until(lambda: not held.fired, 10)
            await asyncio.sleep(0.3)
        told = [(e.outcome, e.found) for e in events if isinstance(e, DeadmanFired)]
        assert told == [("put_back", None)]
        assert again.armed == {}


@pytest.mark.parametrize("arming", ["refused", "no_answer", "no_entry_left"])
async def test_a_change_the_dead_man_isnt_armed_for_isnt_made(
    stocked: SimPump,
    gateway_with_psk: Gateway,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    arming: str,
) -> None:
    """On a route with a dead-man, a change it doesn't confirm is armed for isn't written:
    the gateway refused it, didn't answer, or has no entry left."""
    p = await over_the_protocol(gateway_with_psk, tmp_path)
    async with served(p) as link:
        client = p._transport
        assert isinstance(client, TgwClient)
        assert client.deadman is not None
        if arming == "no_entry_left":
            client.deadman = dataclasses.replace(client.deadman, entries=1)
            other = await act_from(link, "addition/stop_temp", "set", {"value": -10}, 5)
            assert other.stage == "device_accepted"
        else:

            async def arm(entry: int, *args: object) -> Armed | None:
                if arming == "no_answer":
                    return None
                return Armed(entry=entry, result=ArmResult.REFUSED, detail=ArmRefusal.NOT_GRANTED)

            monkeypatch.setattr(client, "arm", arm)
        fate = await act_from(link, "cs1/heating.offset", "set", {"value": 3}, -4)
        assert fate.stage == "dropped"
        assert "dead-man" in str(fate.detail)
        assert 47011 not in [r for r, _ in stocked.taken_writes]
        assert all(register != 47011 for register, _, _ in armed(gateway_with_psk))
    assert signed(stocked.registers[47011]) == -4


async def test_a_report_outlives_a_stop_before_the_core_was_told(
    stocked: SimPump, gateway_with_psk: Gateway, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The plugin stops after taking a report in and before the core heard it: the gateway
    keeps the report, and the plugin started again still knows whose it is."""
    held = gateway_with_psk.control.deadman
    assert held is not None
    async with await Database.open(tmp_path / "db.sqlite") as db:
        state = PluginStore(db, "pump")
        p = await over_the_protocol(gateway_with_psk, tmp_path, state)
        async with served(p) as link:
            assert (await act_from(link, "cs1/heating.offset", "set", {"value": 3}, -4)).stage == (
                "device_accepted"
            )
        held.silence_us = 200_000
        await until(lambda: bool(held.fired), 10)
        stopping = await over_the_protocol(gateway_with_psk, tmp_path, state)
        reached = asyncio.Event()

        def until_told(send: Send) -> Send:
            async def sending(message: Message) -> None:
                if isinstance(message, DeadmanFired):
                    reached.set()
                    await asyncio.Event().wait()  # it stops before the core hears
                await send(message)

            return sending

        events_of = stopping.events
        monkeypatch.setattr(stopping, "events", lambda send: events_of(until_told(send)))
        async with served(stopping):
            await asyncio.wait_for(reached.wait(), 10)
        assert held.fired  # not acknowledged
        events: list[object] = []
        again = await over_the_protocol(gateway_with_psk, tmp_path, state)
        async with served(again, events):
            await until(lambda: not held.fired, 10)
        told = [(e.lever, e.outcome) for e in events if isinstance(e, DeadmanFired)]
        assert told == [("hp1/cs1/heating.offset", "put_back")]
        assert again.reported == {}


async def test_a_baseline_chosen_anew_is_what_the_dead_man_puts_back(
    stocked: SimPump, gateway_with_psk: Gateway, tmp_path: Path
) -> None:
    """Nothing is written: the entry is armed again to put back the new baseline, and
    disarmed where the setting is at it already."""
    p = await over_the_protocol(gateway_with_psk, tmp_path)
    async with served(p) as link:
        assert (await act_from(link, "cs1/heating.offset", "set", {"value": 3}, -4)).stage == (
            "device_accepted"
        )
        writes = len(stocked.taken_writes)
        assert (await act(link, "cs1/heating.offset", "baseline", {"value": 1})).stage == (
            "device_accepted"
        )
        assert armed(gateway_with_psk) == [(47011, 3, 1)]
        assert (await act(link, "cs1/heating.offset", "baseline", {"value": 3})).stage == (
            "device_accepted"
        )
        assert armed(gateway_with_psk) == []
        assert len(stocked.taken_writes) == writes
    assert p.armed == {}


async def test_a_dead_man_kept_by_an_earlier_version_is_read(tmp_path: Path) -> None:
    """It kept one entry per lever, not a list."""
    async with await Database.open(tmp_path / "db.sqlite") as db:
        state = PluginStore(db, "pump")
        kept = {"entry": 2, "register": 47011, "expected": 3, "back": 0xFFFF_FFFC}
        await state.save({"deadman": {"hp1/cs1/heating.offset": kept}})
        p = NibePlugin(NibeGateway(host="127.0.0.1"), state=state)
        await p._restore()
    assert p.armed == {"hp1/cs1/heating.offset": [kept]}


async def uncertain(register: int, raw: int) -> WriteOutcome:
    """A write whose outcome isn't known, and that didn't reach the pump."""
    return WriteOutcome(register, raw, WriteResult.UNKNOWN, "no answer", clock.monotonic(), ())


async def test_a_number_still_owed_a_report_isnt_given_to_a_new_value(
    stocked: SimPump, gateway_with_psk: Gateway, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The gateway puts the offset back from one of the lever's entries while the other is
    still under way as Thermaestro returns. A change made before the core is told gets a
    number of its own: clearing up the old entries leaves it armed."""
    held = gateway_with_psk.control.deadman
    assert held is not None
    async with await Database.open(tmp_path / "db.sqlite") as db:
        state = PluginStore(db, "pump")
        p = await over_the_protocol(gateway_with_psk, tmp_path, state)
        async with served(p) as link:
            assert (await act_from(link, "cs1/heating.offset", "set", {"value": 3}, -4)).stage == (
                "device_accepted"
            )
            p._send_write = uncertain  # type: ignore[method-assign]
            await act_from(link, "cs1/heating.offset", "set", {"value": 5}, -4)
            assert armed(gateway_with_psk) == [(47011, 3, 0xFFFF_FFFC), (47011, 5, 0xFFFF_FFFC)]
        stalled: list[tuple[object, Request]] = []
        submit = held._submit

        def stall_entry_1(client: object, request: Request, now: int) -> None:
            if held._current is not None and held._current.number == 1:
                stalled.append((client, request))  # its read waits on the bus
            else:
                submit(client, request, now)

        held._submit = stall_entry_1
        held.silence_us = 200_000
        await until(lambda: 0 in held.fired and bool(stalled), 10)
        held.silence_us = 900_000_000
        assert signed(stocked.registers[47011]) == -4
        again = await over_the_protocol(gateway_with_psk, tmp_path, state)
        reached, deliver = asyncio.Event(), asyncio.Event()

        def until_delivered(send: Send) -> Send:
            async def sending(message: Message) -> None:
                if isinstance(message, DeadmanFired) and not deliver.is_set():
                    reached.set()
                    await deliver.wait()
                await send(message)

            return sending

        events_of = again.events
        monkeypatch.setattr(again, "events", lambda send: events_of(until_delivered(send)))
        events: list[object] = []
        async with served(again, events) as link:
            await asyncio.wait_for(reached.wait(), 10)
            fate = await act_from(link, "cs1/heating.offset", "set", {"value": 7}, -4)
            assert fate.stage == "device_accepted"
            for client, request in stalled:  # entry 1 finds 7, not its 5, and leaves it
                submit(client, request, now_us())
            await until(lambda: 1 not in held.fired and not held._current, 10)
            deliver.set()
            await until(lambda: not held.fired, 10)
            await asyncio.sleep(0.3)
            assert armed(gateway_with_psk) == [(47011, 7, 0xFFFF_FFFC)]
        told = [(e.lever, e.outcome) for e in events if isinstance(e, DeadmanFired)]
        assert told == [("hp1/cs1/heating.offset", "put_back")]
        held.silence_us = 200_000
        await until(lambda: signed(stocked.registers[47011]) == -4, 10)


async def test_an_entry_kept_but_not_armed_in_this_session_is_armed_before_a_change(
    stocked: SimPump, gateway_with_psk: Gateway, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The gateway restarted without its state, and arming the entries kept failed: a change
    to a value one of them is for is armed first, and not made while it can't be. They are
    armed again soon, the gateway answering."""
    monkeypatch.setattr(plugin_module, "DEADMAN_RETRY_S", 0.2)
    held = gateway_with_psk.control.deadman
    assert held is not None
    async with await Database.open(tmp_path / "db.sqlite") as db:
        state = PluginStore(db, "pump")
        p = await over_the_protocol(gateway_with_psk, tmp_path, state)
        async with served(p) as link:
            assert (await act_from(link, "cs1/heating.offset", "set", {"value": 3}, -4)).stage == (
                "device_accepted"
            )
            p._send_write = uncertain  # type: ignore[method-assign]
            await act_from(link, "cs1/heating.offset", "set", {"value": 5}, -4)
        held.armed.clear()
        arm = held.arm

        def refused(msg: Arm, entries: int) -> Armed:
            return Armed(
                entry=msg.entry, result=ArmResult.REFUSED, detail=ArmRefusal.NOT_GRANTED, id=msg.id
            )

        monkeypatch.setattr(held, "arm", refused)
        again = await over_the_protocol(gateway_with_psk, tmp_path, state)
        async with served(again) as link:
            fate = await act_from(link, "cs1/heating.offset", "set", {"value": 5}, -4)
            assert fate.stage == "dropped"
            assert "dead-man" in str(fate.detail)
            assert signed(stocked.registers[47011]) == 3
            monkeypatch.setattr(held, "arm", arm)
            both = [(47011, 3, 0xFFFF_FFFC), (47011, 5, 0xFFFF_FFFC)]
            await until(lambda: armed(gateway_with_psk) == both, 5)
            fate = await act_from(link, "cs1/heating.offset", "set", {"value": 5}, -4)
            assert fate.stage == "device_accepted"
            assert armed(gateway_with_psk) == [(47011, 5, 0xFFFF_FFFC)]


@pytest.mark.parametrize("disarming", ["refused", "no_answer"])
async def test_a_value_held_taken_as_the_baseline_waits_for_the_gateway_to_disarm(
    stocked: SimPump,
    gateway_with_psk: Gateway,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    disarming: str,
) -> None:
    """The value the pump holds chosen as the baseline: refused unless the gateway confirms
    its entry disarmed, and the lever's other entries then left putting back the old one."""
    p = await over_the_protocol(gateway_with_psk, tmp_path)
    async with served(p) as link:
        assert (await act_from(link, "cs1/heating.offset", "set", {"value": 3}, -4)).stage == (
            "device_accepted"
        )
        p._send_write = uncertain  # type: ignore[method-assign]
        await act_from(link, "cs1/heating.offset", "set", {"value": 5}, -4)
        client = p._transport
        assert isinstance(client, TgwClient)

        async def disarm(entry: int) -> Armed | None:
            if disarming == "no_answer":
                return None
            return Armed(entry=entry, result=ArmResult.REFUSED, detail=ArmRefusal.NOT_GRANTED)

        monkeypatch.setattr(client, "disarm", disarm)
        fate = await act(link, "cs1/heating.offset", "baseline", {"value": 3})
        assert fate.stage == "dropped"
        assert "dead-man" in str(fate.detail)
        assert armed(gateway_with_psk) == [(47011, 3, 0xFFFF_FFFC), (47011, 5, 0xFFFF_FFFC)]
        assert [k["expected"] for k in p.armed["hp1/cs1/heating.offset"]] == [3, 5]


@pytest.mark.parametrize("answering", [False, True])
async def test_a_refused_baseline_the_gateway_may_hold_is_told_with_its_put_back(
    stocked: SimPump,
    gateway_with_psk: Gateway,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    answering: bool,
) -> None:
    """A baseline chosen anew reaches the gateway, but its answer is lost, and so is the
    arming that puts the old one back: the change is refused, and the gateway may put back
    the refused baseline. Its put-back names that value besides its own, for the core to read
    which it was. Answering again before its silence, the gateway holds the old baseline, and the
    put-back is told as one."""
    monkeypatch.setattr(plugin_module, "DEADMAN_RETRY_S", 0.2)
    held = gateway_with_psk.control.deadman
    assert held is not None
    async with await Database.open(tmp_path / "db.sqlite") as db:
        state = PluginStore(db, "pump")
        p = await over_the_protocol(gateway_with_psk, tmp_path, state)
        async with served(p) as link:
            assert (await act_from(link, "cs1/heating.offset", "set", {"value": 3}, -4)).stage == (
                "device_accepted"
            )
            client = p._transport
            assert isinstance(client, TgwClient)
            arm, sent = client.arm, [0]

            async def answers_lost(entry: int, *args: Any) -> Armed | None:
                sent[0] += 1
                if sent[0] == 1:
                    await arm(entry, *args)  # it reaches the gateway
                return None

            monkeypatch.setattr(client, "arm", answers_lost)
            fate = await act(link, "cs1/heating.offset", "baseline", {"value": 1})
            assert fate.stage == "dropped"
            assert armed(gateway_with_psk) == [(47011, 3, 1)]
            if answering:
                monkeypatch.setattr(client, "arm", arm)
                await until(lambda: armed(gateway_with_psk) == [(47011, 3, 0xFFFF_FFFC)], 5)
                await until(lambda: not p.may_hold)
        held.silence_us = 200_000
        await until(lambda: bool(held.fired), 10)
        assert signed(stocked.registers[47011]) == (-4 if answering else 1)
        events: list[object] = []
        again = await over_the_protocol(gateway_with_psk, tmp_path, state)
        async with served(again, events):
            await until(lambda: any(isinstance(e, DeadmanFired) for e in events), 10)
        told = next(e for e in events if isinstance(e, DeadmanFired))
        assert (told.outcome, told.put) == ("put_back", [-4] if answering else [-4, 1])
        assert again.may_hold == {}


async def test_a_stop_while_an_arming_is_answered_still_knows_what_the_gateway_may_hold(
    stocked: SimPump, gateway_with_psk: Gateway, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Thermaestro stops after a baseline chosen anew reached the gateway, before its answer
    came: the entry kept says the new one, which the core never took. What the gateway may
    hold was kept before it was sent, so its put-back names both, for the core to read."""
    held = gateway_with_psk.control.deadman
    assert held is not None
    async with await Database.open(tmp_path / "db.sqlite") as db:
        state = PluginStore(db, "pump")
        p = await over_the_protocol(gateway_with_psk, tmp_path, state)
        async with served(p) as link:
            assert (await act_from(link, "cs1/heating.offset", "set", {"value": 3}, -4)).stage == (
                "device_accepted"
            )
            client = p._transport
            assert isinstance(client, TgwClient)
            arm = client.arm

            async def stopped_waiting(entry: int, *args: Any) -> Armed | None:
                await arm(entry, *args)  # it reaches the gateway
                await asyncio.Event().wait()  # Thermaestro stops before the answer is had
                raise AssertionError

            monkeypatch.setattr(client, "arm", stopped_waiting)
            choosing = asyncio.create_task(
                act(link, "cs1/heating.offset", "baseline", {"value": 1})
            )
            await until(lambda: armed(gateway_with_psk) == [(47011, 3, 1)], 10)
            choosing.cancel()
            await asyncio.gather(choosing, return_exceptions=True)
        held.silence_us = 200_000
        await until(lambda: bool(held.fired), 10)
        assert signed(stocked.registers[47011]) == 1
        events: list[object] = []
        again = await over_the_protocol(gateway_with_psk, tmp_path, state)
        async with served(again, events):
            await until(lambda: any(isinstance(e, DeadmanFired) for e in events), 10)
        told = next(e for e in events if isinstance(e, DeadmanFired))
        assert (told.outcome, told.put) == ("put_back", [1, -4])


async def test_a_dead_man_granted_later_is_used_from_then_on(
    stocked: SimPump, gateway_with_psk: Gateway, tmp_path: Path
) -> None:
    """The gateway had no dead-man when the plugin started (one updated meanwhile): granted
    in a later session, the unit is described anew as covered, and from then on what is
    changed is armed, and armed again in each new session."""
    held = gateway_with_psk.control.deadman
    assert held is not None
    gateway_with_psk.control.deadman = None  # not yet
    events: list[object] = []
    p = await over_the_protocol(gateway_with_psk, tmp_path)
    async with served(p, events) as link:
        described = await link.describe(timeout=5)
        unit = next(n for n in described.nodes if n.path == "hp1")
        assert unit.transport is not None
        assert unit.transport.deadman == "unsupported"
        client = p._transport
        assert isinstance(client, TgwClient)
        gateway_with_psk.control.deadman = held  # the gateway updated
        client._lose_session("the gateway restarted", gone=True)

        def covered() -> bool:
            return any(
                isinstance(e, Described)
                and any(
                    n.path == "hp1" and n.transport and n.transport.deadman == "supported"
                    for n in e.nodes
                )
                for e in events
            )

        await until(covered, 10)
        assert (await act_from(link, "cs1/heating.offset", "set", {"value": 3}, -4)).stage == (
            "device_accepted"
        )
        assert armed(gateway_with_psk) == [(47011, 3, 0xFFFF_FFFC)]
        held.armed.clear()  # restarted without its state
        client._lose_session("the gateway restarted", gone=True)
        await until(lambda: armed(gateway_with_psk) == [(47011, 3, 0xFFFF_FFFC)], 10)


async def test_a_disarm_the_gateway_didnt_confirm_is_sent_again(
    stocked: SimPump, gateway_with_psk: Gateway, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An entry for a value replaced stays armed at the gateway while its disarm isn't
    confirmed; it is sent again, so the entry can't put back over a value someone sets."""
    monkeypatch.setattr(plugin_module, "DEADMAN_RETRY_S", 0.2)
    p = await over_the_protocol(gateway_with_psk, tmp_path)
    async with served(p) as link:
        assert (await act_from(link, "cs1/heating.offset", "set", {"value": 3}, -4)).stage == (
            "device_accepted"
        )
        client = p._transport
        assert isinstance(client, TgwClient)
        disarm, lost = client.disarm, [0]

        async def once_lost(entry: int) -> Armed | None:
            if not lost[0]:
                lost[0] = entry + 1
                return None
            return await disarm(entry)

        monkeypatch.setattr(client, "disarm", once_lost)
        assert (await act_from(link, "cs1/heating.offset", "set", {"value": 5}, -4)).stage == (
            "device_accepted"
        )
        assert lost[0]
        await until(lambda: armed(gateway_with_psk) == [(47011, 5, 0xFFFF_FFFC)], 5)
        assert p.disarming == set()


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
        assert stocked.registers[47044] == 350
        assert p.holds == {"hp1/dhw/block": {"register": 47044, "value": 45.0, "mode": 1}}
        assert (await act(link, "dhw/block", "release")).stage == "device_accepted"
        assert stocked.registers[47044] == 450
        assert p.holds == {}
        # A start already as low as the block's: what to put back isn't known.
        stocked.registers[47045] = 350
        stocked.registers[47041] = 0
        fate = await act(link, "dhw/block", "engage")
        assert fate.stage == "dropped"
        assert "already reads 35 °C" in (fate.detail or "")
        # Smart Control has no start of its own.
        stocked.registers[47041] = 4
        fate = await act(link, "dhw/block", "engage")
        assert (fate.stage, fate.detail) == (
            "dropped",
            "Smart Control has no start temperature of its own to lower",
        )
    assert [r for r, _ in stocked.taken_writes] == [47044, 47044]


async def test_the_start_and_stop_under_standard_names(stocked: SimPump, gateway: Gateway) -> None:
    """The current mode's; while the block lowers the start, what its release puts back;
    none in Smart Control. The pool's as they are."""
    stocked.registers.update({47047: 530, 47048: 500, 47049: 470})
    p = plugin(gateway)

    def edges(node: str = "dhw") -> tuple[object, ...]:
        return tuple(p.envelope(f"hp1/{node}/temp.{e}").value for e in ("start", "stop"))

    async with served(p) as link:
        await until(lambda: edges() == (45.0, 50.0))
        await until(lambda: edges("pool1") == (22.0, 28.0))
        points = {pt.path for pt in p.describe().points}
        assert {"hp1/dhw/temp.start", "hp1/dhw/temp.stop", "hp1/pool1/temp.start"} <= points
        assert (await act(link, "dhw/block", "engage")).stage == "device_accepted"
        await until(lambda: p.envelope("hp1/dhw/x.nibe.47044").value == 35.0)
        assert edges() == (45.0, 50.0)
        stocked.registers[47041] = 0
        await until(lambda: edges() == (42.0, 47.0))
        stocked.registers[47041] = 4
        await until(lambda: edges() == (None, None))
        assert p.envelope("hp1/dhw/temp.start").why == "Smart Control has no setting of its own"
        stocked.registers[47041] = 1
        await until(lambda: edges() == (45.0, 50.0))
        await act(link, "dhw/block", "release")


async def test_a_blocks_release_outlives_a_restart(
    stocked: SimPump, gateway: Gateway, tmp_path: Path
) -> None:
    async with await Database.open(tmp_path / "db.sqlite") as db:
        state = PluginStore(db, "pump")
        async with served(plugin(gateway, state)) as link:
            assert (await act(link, "dhw/block", "engage")).stage == "device_accepted"
        assert stocked.registers[47044] == 350
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
            "Economy's start temperature (47045) lowered to 35.0 °C"
        ]
        await act(link, "dhw/block", "release")
        await act(link, "dhw/block", "engage")
    assert (stocked.registers[47044], stocked.registers[47045]) == (450, 350)


async def test_an_engage_puts_a_stray_block_back_first(stocked: SimPump, gateway: Gateway) -> None:
    """A block left engaged on another mode's start is put back before the next one."""
    p = plugin(gateway)
    async with served(p) as link:
        await act(link, "dhw/block", "engage")
        stocked.registers[47041] = 0
        assert (await act(link, "dhw/block", "engage")).stage == "device_accepted"
    assert (stocked.registers[47044], stocked.registers[47045]) == (450, 350)
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


async def test_a_persons_write_of_the_pumps_own_settings(
    stocked: SimPump, gateway: Gateway
) -> None:
    """Any writable register, even one no lever writes, read back though no point describes
    it; never the word order, a register a hold has engaged, or a value it doesn't take."""
    stocked.registers[47134] = 0  # the hot-water period, as NibePi's learning left it
    p = plugin(gateway)
    async with served(p) as link:
        assert "write" in p.features

        async def write(point: str, value: float) -> Fate:
            return [f async for f in link.write(f"hp1/{point}", value, timeout=20)][-1]

        accepted = await write("x.nibe.47134", 30)
        assert accepted.stage == "device_accepted"
        back = await link.read(["hp1/x.nibe.47134"], after=accepted.t, timeout=10)
        assert (back.values[0].value, back.values[0].quality) == (30, "good")
        assert (await act(link, "dhw/block", "engage")).stage == "device_accepted"
        cases = [
            ("x.nibe.48852", 1, "the word order other clients decode by is never written"),
            ("x.nibe.47044", 30, "hp1/dhw/block holds it now"),
            ("x.nibe.47011", 11, "outside the register's range"),
            ("x.nibe.40004", 1, "read-only"),
            ("x.nibe.1", 1, "not one of the pump's registers"),
            ("cs1/heating.offset", 1, "not one of the pump's registers"),
        ]
        for point, value, why in cases:
            fate = await write(point, value)
            assert fate.stage == "dropped", point
            assert why in (fate.detail or ""), (point, fate.detail)
    assert stocked.registers[47134] == 30
    assert [r for r, _ in stocked.taken_writes] == [47134, 47044]


async def test_writes_wait_for_a_route_that_has_them() -> None:
    """Writing isn't built for Modbus TCP: an S-series pump's plugin doesn't offer it."""
    s = NibePlugin(NibeGateway(protocol="modbus-tcp", host="192.0.2.30", model="S1255"))
    assert "write" not in s.features


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
    prepare: Any = None,
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
        # The household has confirmed the pump's own schedule and price adaption are off.
        confirmed = {f"pump:hp1/{lv}": (SCHEDULE, PRICED) for lv in levers}
        await db.put(
            Control(levers={f"pump:hp1/{k}": v for k, v in levers.items()}, confirmed_off=confirmed)
        )
        if prepare is not None:
            await prepare(db)

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
            while stocked.registers[47045] != 350 or stocked.registers[47044] != 450:
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
            ("addition/max_power", "set", {"value": 3.5}),
        ]
        for lever, op, params in requests:
            result = await core.executor.act(f"pump:hp1/{lever}", op, params, who="planner")
            outcomes.append(result.outcome)

    shadow = (
        "cs1/heating.offset",
        "dhw/block",
        "dhw/mode",
        "addition/stop_temp",
        "addition/max_power",
    )
    await daemon(tmp_path, gateway, dict.fromkeys(shadow, "shadow"), started)
    assert outcomes == ["shadowed"] * 5
    assert stocked.taken_writes == []


def test_levers_tried_on_a_pump_are_verified_on_that_model_and_firmware_only() -> None:
    """What the rig showed on a real pump counts for that model on that firmware; elsewhere,
    and where the firmware isn't known, as documented."""
    maps = load("bus")
    f1245, f1155 = maps.model("F1245"), maps.model("F1155")
    documented = Knowledge[bool](value=True, known="documented", basis="a manual")
    for path in ("cs1/heating.offset", "dhw/mode", "dhw/block", "dhw/boost_once"):
        found = profile.tried(path, f1245, "9721R4", documented)
        assert (found.known, found.value) == ("verified", True)
        assert "F1245, firmware 9721R4" in str(found.basis)
        assert profile.tried(path, f1155, "9721R4", documented) == documented
        assert profile.tried(path, f1245, "9800R1", documented) == documented
        assert profile.tried(path, f1245, None, documented) == documented
    # Only climate system 1's offset was tried.
    assert profile.tried("cs2/heating.offset", f1245, "9721R4", documented) == documented
    assert profile.tried("addition/stop_temp", f1245, "9721R4", documented) == documented


async def test_a_pool_block_left_by_an_earlier_version_is_released(
    stocked: SimPump, gateway: Gateway, tmp_path: Path
) -> None:
    """An earlier version switched pool heating off and kept the hold: the lever is offered
    again for its release only, and goes once released. Switched on meanwhile by someone
    else, it is let be: nothing is written."""
    for meanwhile, writes in ((0, [(48094, 1)]), (1, [])):
        async with await Database.open(tmp_path / f"db{meanwhile}.sqlite") as db:
            state = PluginStore(db, "pump")
            await state.save({"holds": {"hp1/pool1/block": {"register": 48094, "value": 1}}})
            stocked.registers[48094] = meanwhile
            stocked.taken_writes.clear()
            p = plugin(gateway, state)
            async with served(p) as link:
                offered = {lv.path for lv in (await link.describe(timeout=5)).levers}
                assert "hp1/pool1/block" in offered
                refused = await act(link, "pool1/block", "engage")
                assert (refused.stage, refused.detail) == (
                    "dropped",
                    "no longer engaged; offered only to release what was engaged before",
                )
                assert (await act(link, "pool1/block", "release")).stage == "device_accepted"
                after = {lv.path for lv in (await link.describe(timeout=5)).levers}
                assert "hp1/pool1/block" not in after
            assert stocked.registers[48094] == 1
            assert [(r, v) for r, v in stocked.taken_writes] == writes
            assert (await state.load())["holds"] == {}


async def test_an_upgrade_puts_back_a_pool_block_an_earlier_version_left(
    stocked: SimPump, gateway: Gateway, tmp_path: Path
) -> None:
    """Thermaestro stopped uncleanly with pool heating switched off, and starts again as a
    version without the pool block: the held claim and the plugin's hold both settle, the
    heating on again. Switched on meanwhile by someone else, it is left as it is."""
    from thermaestro.core.executor import Claim, _store_claim

    ref = "pump:hp1/pool1/block"
    for meanwhile in (0, 1):
        stocked.registers[48094] = meanwhile
        stocked.taken_writes.clear()

        async def left_held(db: Database) -> None:
            claim = Claim(ref, 1.0, None, "control", held=True, last_t=1.0)
            await db.run(lambda t: _store_claim(t, claim))
            hold = {"hp1/pool1/block": {"register": 48094, "value": 1}}
            await PluginStore(db, "pump").save({"holds": hold})

        async def started(core: Core) -> None:
            async with asyncio.timeout(30):
                while (claim := core.executor.claims.get(ref)) is None or claim.changed:
                    await asyncio.sleep(0.1)
            assert core.executor.put_back([ref])

        (tmp_path / str(meanwhile)).mkdir()
        await daemon(
            tmp_path / str(meanwhile), gateway, {"pool1/block": "control"}, started, left_held
        )
        assert stocked.registers[48094] == 1
        assert [(r, v) for r, v in stocked.taken_writes] == ([(48094, 1)] if meanwhile == 0 else [])
