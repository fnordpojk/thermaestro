"""The rig's checks: one lever each (the pool's three together), tried on the pump.

Each says what it will write, takes the lever over through the executor, watches what the
pump does where there is something to see, puts the lever back, and, where a person must
judge, asks. In a read-only run each goes as far as the executor's shadow goes.
"""

import asyncio
import contextlib
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ... import durations
from ...cap.defaults import assume
from ...core.executor import same
from ...store import NibeGateway
from .. import profile
from ..transport import connect
from ..transport.base import Transport
from .bench import FINE, READY_S, Ask, Bench, Declined, Options, Report, RigError, Say

EFFECT_S = 900.0
"""How long the pump may take to answer a change: a charge starting, the addition
stopping."""
HELD_S = 900.0
"""How long the block must hold a charge off, once the charge sensor is below the start,
to count."""

HOT_WATER = {
    "charge sensor": "dhw/temp.charge",
    "top": "dhw/temp.top",
    "start": "dhw/temp.start",
    "demand": "demand",
}
ADDITION = {
    "addition": "addition/power",
    "demand": "demand",
    "outdoor mean": "outdoor.temp.mean",
}
NEIGHBOR = {"eco": "normal", "normal": "eco", "lux": "normal", "smart": "eco"}
"""The mode `mode` tries by default: a lower one where there is one, so no charge starts."""


@dataclass(frozen=True)
class Check:
    about: str
    levers: Callable[[Options], tuple[str, ...]]
    """Below the unit."""
    points: Callable[[Options], tuple[str, ...]]
    """The values it watches, besides what its levers read back and rest on."""
    run: Callable[[Bench, Options], Awaitable[None]]


def _charging(bench: Bench) -> bool:
    return bool(bench.value("demand") == "dhw")


async def _no_charge_running(bench: Bench, o: Options) -> bool:
    """Wait for a running charge to end, so a check starts from a quiet tank."""
    if not bench.write or not _charging(bench):
        return True
    bench.say("A hot-water charge is running: waiting for it to end.")
    took = await bench.watch(lambda: not _charging(bench), minutes=o.minutes, show=HOT_WATER)
    if took is None:
        span = durations.text(o.minutes * 60)
        bench.step(f"a charge was still running after {span}", "not seen")
        return False
    return True


def _register(path: str, bench: Bench) -> str:
    return ", ".join(t.removeprefix("x.nibe.") for t in bench.need(path).touches)


# --- offset ---------------------------------------------------------------------------------


async def offset(bench: Bench, o: Options) -> None:
    path = f"cs{o.system}/heating.offset"
    found = bench.setting(path)
    if not isinstance(found, int | float):
        raise RigError(f"{path} can't be read now")
    target = found + 1 if found < 10 else found - 1
    await bench.consent(
        [
            f"the heating offset of climate system {o.system} ({_register(path, bench)}):"
            f" from {found:g} to {target:g}, then back to {found:g}"
        ],
        [path],
    )
    result = await bench.act(path, "set", {"value": target}, why="the rig: a new offset")
    if result.outcome in FINE:
        await bench.put_back([path])


# --- mode -----------------------------------------------------------------------------------


async def mode(bench: Bench, o: Options) -> None:
    path = "dhw/mode"
    found = bench.setting(path)
    if isinstance(found, int | float):  # one the lever doesn't offer: Smart Control
        found = {v: k for k, v in profile.MODES.items()}.get(int(found), found)
    target = o.mode or NEIGHBOR.get(str(found), "normal")
    if target == found:
        raise RigError(f"the hot-water mode is {found} already: name another with --mode")
    await bench.consent(
        [
            f"the hot-water mode ({_register(path, bench)}): from {found} to {target},"
            f" then back to {found}"
        ],
        [path],
    )
    result = await bench.act(path, "set", {"value": target}, why="the rig: another mode")
    if result.outcome not in FINE:
        return
    if bench.write:
        start = bench.value("dhw/temp.start")
        shown = "-" if start is None else f"{start:g} °C"
        bench.step(f"the start temperature now: {shown}, {target}'s", "seen")
    await bench.put_back([path])


# --- block ----------------------------------------------------------------------------------


async def block(bench: Bench, o: Options) -> None:
    path = "dhw/block"
    lever = bench.need(path)
    mode_now = bench.envelope(f"dhw/x.nibe.{profile.HOT_WATER_MODE}")
    raw = None if mode_now is None else mode_now.raw
    register = None if not isinstance(raw, int | float) else profile.MODE_STARTS.get(int(raw))
    if register is None:
        raise RigError(lever.implementation.how or "the hot-water mode can't be blocked")
    start = bench.value(f"dhw/x.nibe.{register}")
    if not isinstance(start, int | float):
        raise RigError(f"the start temperature ({register}) can't be read now")
    await bench.consent(
        [f"{lever.implementation.how}, then back to {start:g} °C"],
        [path],
    )
    if not await _no_charge_running(bench, o):
        return
    engaged = await bench.act(path, "engage", why="the rig: hold off the pump's charges")
    if engaged.outcome not in FINE:
        return
    if not bench.write:
        await bench.put_back([path])
        return
    lowered = await bench.fresh(f"dhw/x.nibe.{register}")
    bench.step(
        f"the start temperature ({register}) reads {profile.BLOCK_START:.1f} °C",
        "seen",
        f"it reads {lowered}",
        same(lowered, profile.BLOCK_START),
    )

    def below() -> bool:
        charge = bench.value("dhw/temp.charge")
        return isinstance(charge, int | float) and charge < start

    span = durations.text(o.minutes * 60)
    bench.say(
        f"Watching for up to {span}: the charge sensor falling below {start:g} °C, where the"
        " pump would start a charge. Ctrl-C ends the check and puts everything back."
    )
    held = False
    took = await bench.watch(lambda: below() or _charging(bench), minutes=o.minutes, show=HOT_WATER)
    if took is None:
        bench.step(
            f"the charge sensor stayed at or above {start:g} °C for {span}: nothing to hold off",
            "not seen",
        )
    elif _charging(bench):
        _started_anyway(bench)
    else:
        bench.say(
            f"The charge sensor is below {start:g} °C. Watching {durations.text(HELD_S)} more"
            " for a charge."
        )
        if await bench.watch(lambda: _charging(bench), minutes=HELD_S / 60, show=HOT_WATER):
            _started_anyway(bench)
        else:
            charge = bench.value("dhw/temp.charge")
            bench.step(
                f"held off: the charge sensor at {charge} °C, below the start {start:g} °C,"
                f" for {durations.text(HELD_S)} with no charge",
                "seen",
                passed=True,
            )
            held = True
    await bench.put_back([path])
    back = await bench.fresh(f"dhw/x.nibe.{register}")
    bench.step(
        f"the start temperature ({register}) is back at {start:g} °C",
        "seen",
        f"it reads {back}",
        same(back, start),
    )
    summary = "The block was released."
    if held:
        after = await bench.watch(lambda: _charging(bench), minutes=EFFECT_S / 60, show=HOT_WATER)
        if after is None:
            bench.step(f"no charge within {durations.text(EFFECT_S)} of the release", "not seen")
        else:
            bench.step(f"a charge started {durations.text(after)} after the release", "seen")
        summary = "The pump held its charge off while the start was lowered" + (
            ", and charged once it was put back." if after is not None else "."
        )
    await bench.judge(summary)


def _started_anyway(bench: Bench) -> None:
    charge = bench.value("dhw/temp.charge")
    bench.step(
        f"a charge started while the block held, the charge sensor at {charge} °C",
        "seen",
        "the periodic increase or the pump's own schedule may start one",
        False,
    )


# --- boost ----------------------------------------------------------------------------------


async def boost(bench: Bench, o: Options) -> None:
    path = "dhw/boost_once"
    await bench.consent(
        [f"{_register(path, bench)} = {profile.BOOST_ONCE}: one extra charge of hot water now"],
        [path],
    )
    if not await _no_charge_running(bench, o):
        return
    top = bench.value("dhw/temp.top")
    fired = await bench.act(path, "fire", why="the rig: one extra charge")
    if fired.outcome not in FINE or not bench.write:
        return
    took = await bench.watch(lambda: _charging(bench), minutes=EFFECT_S / 60, show=HOT_WATER)
    if took is None:
        bench.step(f"no charge within {durations.text(EFFECT_S)}", "not seen", passed=False)
        await bench.act(path, "cancel", why="the rig: no charge came")
        await bench.judge("No charge started.")
        return
    bench.step(f"a charge started {durations.text(took)} after", "seen", passed=True)
    ended = await bench.watch(lambda: not _charging(bench), minutes=o.minutes, show=HOT_WATER)
    after = bench.value("dhw/temp.top")
    if ended is None:
        span = durations.text(o.minutes * 60)
        bench.step(f"the charge was still running after {span}", "seen")
        await bench.act(path, "cancel", why="the rig: the check's time is up")
    else:
        bench.step(
            f"the charge ended after {durations.text(ended)}; the top went from {top} to"
            f" {after} °C",
            "seen",
            passed=True,
        )
    flag = await bench.fresh(f"dhw/x.nibe.{profile.BOOST}")
    bench.step(f"{profile.BOOST} reads {flag} afterwards", "seen")
    await bench.judge(f"One extra charge: the top from {top} to {after} °C.")


# --- addition -------------------------------------------------------------------------------


def _addition_running(bench: Bench) -> bool:
    power = bench.value("addition/power")
    return isinstance(power, int | float) and power > 0


async def addition(bench: Bench, o: Options) -> None:
    path = f"addition/{o.setting}"
    lever = bench.need(path)
    found = bench.setting(path)
    limits = assume(lever).ranges.get("value")
    if not isinstance(found, int | float) or limits is None or limits.min is None:
        raise RigError(f"{path} can't be read now")
    target = limits.min
    what = {
        "stop_temp": "the addition's stop temperature",
        "max_power": "the addition's most power",
    }[o.setting]
    await bench.consent(
        [
            f"{what} ({_register(path, bench)}): from {found:g} to {target:g}, which keeps the"
            f" addition off, then back to {found:g}"
        ],
        [path],
    )
    if bench.write and not _addition_running(bench):
        span = durations.text(o.minutes * 60)
        bench.say(f"The addition isn't running. Waiting up to {span} for it to start.")
        ran = await bench.watch(lambda: _addition_running(bench), minutes=o.minutes, show=ADDITION)
        if ran is None:
            bench.step(f"the addition didn't run in {span}: nothing to see", "not seen")
            return
    demand = bench.value("demand")
    result = await bench.act(path, "set", {"value": target}, why="the rig: keep the addition off")
    if result.outcome not in FINE:
        return
    if not bench.write:
        await bench.put_back([path])
        return
    took = await bench.watch(
        lambda: not _addition_running(bench), minutes=EFFECT_S / 60, show=ADDITION
    )
    if took is None:
        bench.step(
            f"the addition still ran after {durations.text(EFFECT_S)}",
            "seen",
            f"demand: {demand}",
            False,
        )
    else:
        bench.step(
            f"the addition stopped {durations.text(took)} after",
            "seen",
            f"demand: {demand}",
            True,
        )
    await bench.put_back([path])
    again = await bench.watch(
        lambda: _addition_running(bench), minutes=EFFECT_S / 60, show=ADDITION
    )
    if again is None:
        bench.step(f"the addition didn't run again within {durations.text(EFFECT_S)}", "not seen")
    else:
        bench.step(f"the addition ran again {durations.text(again)} after the put-back", "seen")
    await bench.judge(f"The addition with {what} at {target:g}, then at {found:g} again.")


# --- pool -----------------------------------------------------------------------------------


async def pool(bench: Bench, o: Options) -> None:
    node = f"pool{o.pool}"
    start_path, stop_path, block_path = (
        f"{node}/{n}" for n in ("start_temp", "stop_temp", "block")
    )
    start, stop = bench.setting(start_path), bench.setting(stop_path)
    if not isinstance(start, int | float) or not isinstance(stop, int | float):
        raise RigError(f"{node}'s start and stop can't be read now")
    new_start = start - 1 if start - 1 >= 5 else start + 1
    new_stop = stop + 1 if stop + 1 <= 80 else stop - 1
    if not new_start < stop or not start < new_stop:
        raise RigError(f"{node}'s start ({start:g}) and stop ({stop:g}) are too close to try")
    activated = _register(block_path, bench)
    await bench.consent(
        [
            f"{node}'s start temperature ({_register(start_path, bench)}): from {start:g} to"
            f" {new_start:g}, then back",
            f"{node}'s stop temperature ({_register(stop_path, bench)}): from {stop:g} to"
            f" {new_stop:g}, then back",
            f"{node}'s heating switched off ({activated} = 0), then on again",
        ],
        [start_path, stop_path, block_path],
    )
    for path, value in ((start_path, new_start), (stop_path, new_stop)):
        result = await bench.act(path, "set", {"value": value}, why="the rig: a new pool setting")
        if result.outcome not in FINE:
            return
        await bench.put_back([path])
    was = bench.value(f"{node}/x.nibe.{activated}")
    heating = bench.value("demand") == "pool"
    engaged = await bench.act(block_path, "engage", why="the rig: hold off the pool's heating")
    if engaged.outcome not in FINE:
        return
    if not bench.write:
        await bench.put_back([block_path])
        return
    off = await bench.fresh(f"{node}/x.nibe.{activated}")
    bench.step(f"{activated} reads 0", "seen", f"it reads {off}", same(off, 0))
    if heating:
        show = {"pool": f"{node}/temp", "demand": "demand"}
        took = await bench.watch(
            lambda: bench.value("demand") != "pool", minutes=EFFECT_S / 60, show=show
        )
        if took is None:
            bench.step(
                f"the pool still heated after {durations.text(EFFECT_S)}", "seen", passed=False
            )
        else:
            stopped = f"the pool's heating stopped {durations.text(took)} after"
            bench.step(stopped, "seen", passed=True)
    else:
        bench.step("the pool wasn't heating: nothing to hold off", "not seen")
    await bench.put_back([block_path])
    on = await bench.fresh(f"{node}/x.nibe.{activated}")
    bench.step(f"{activated} is back at {was}", "seen", f"it reads {on}", same(on, was))
    await bench.judge(f"{node}'s start, stop and heating, each changed and put back.")


CHECKS: dict[str, Check] = {
    "offset": Check(
        "the heating offset: +1 (or -1 at the top), read back, put back",
        lambda o: (f"cs{o.system}/heating.offset",),
        lambda o: (),
        offset,
    ),
    "mode": Check(
        "the hot-water mode: another mode, read back, put back",
        lambda o: ("dhw/mode",),
        lambda o: ("dhw/temp.start",),
        mode,
    ),
    "block": Check(
        "the hot-water block: engaged until the pump would have charged, then released",
        lambda o: ("dhw/block",),
        lambda o: (
            *HOT_WATER.values(),
            f"dhw/x.nibe.{profile.HOT_WATER_MODE}",
            *(f"dhw/x.nibe.{r}" for r in profile.MODE_STARTS.values()),
        ),
        block,
    ),
    "boost": Check(
        "one extra charge of hot water, watched until it ends",
        lambda o: ("dhw/boost_once",),
        lambda o: tuple(HOT_WATER.values()),
        boost,
    ),
    "addition": Check(
        "the addition's stop temperature (or --setting max_power) set to keep it off while it"
        " runs, then put back",
        lambda o: (f"addition/{o.setting}",),
        lambda o: tuple(ADDITION.values()),
        addition,
    ),
    "pool": Check(
        "a pool's start, stop and heating, each changed and put back",
        lambda o: tuple(f"pool{o.pool}/{n}" for n in ("start_temp", "stop_temp", "block")),
        lambda o: ("demand", f"pool{o.pool}/temp"),
        pool,
    ),
}


async def run(
    name: str,
    gateway: NibeGateway,
    *,
    state: Path,
    write: bool,
    options: Options,
    say: Say,
    ask: Ask,
    psk: bytes | None = None,
    connect_fn: Callable[..., Awaitable[Transport]] = connect,
    transport_settings: dict[str, Any] | None = None,
    identify_timeout_s: float = 40.0,
    ready_s: float = READY_S,
    plugin_options: Mapping[str, Any] | None = None,
    interrupt: asyncio.Event | None = None,
) -> Report:
    """Run a check, or `restore`, and report what happened. `interrupt` ends a check early;
    what it changed is put back either way."""
    bench = Bench(
        gateway,
        state=state,
        write=write,
        say=say,
        ask=ask,
        psk=psk,
        connect_fn=connect_fn,
        transport_settings=transport_settings,
        identify_timeout_s=identify_timeout_s,
        ready_s=ready_s,
        plugin_options=plugin_options,
    )
    check = CHECKS.get(name)
    levers = check.levers(options) if check is not None else ()
    try:
        left = await bench.open()
        pump = bench.pump()
        say(f"Identified the pump: {pump['model']}, firmware {pump['firmware']}.")
        if check is None:
            await _restore(bench, left)
        else:
            if left:
                raise RigError(
                    f"an earlier run left {', '.join(left)} changed: put it back first with"
                    " `thermaestro rig restore --write`"
                )
            for path in levers:
                bench.need(path)
            await bench.ready(levers, check.points(options))
            await _until_interrupted(bench, check.run(bench, options), interrupt)
    except Declined:
        bench.step("go ahead", "declined: nothing was written")
    finally:
        await bench.close()
    return bench.report(name, options, levers)


async def _until_interrupted(
    bench: Bench, work: Awaitable[None], interrupt: asyncio.Event | None
) -> None:
    task = asyncio.ensure_future(work)
    waiting = asyncio.ensure_future(interrupt.wait()) if interrupt is not None else None
    try:
        await asyncio.wait(
            {task} | ({waiting} if waiting else set()), return_when=asyncio.FIRST_COMPLETED
        )
    finally:
        if waiting is not None:
            waiting.cancel()
    if task.done():
        task.result()
        return
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    bench.step("stopped", "stopped by the person", "what was changed is put back")


async def _restore(bench: Bench, left: list[str]) -> None:
    """Put back what an earlier run left changed: with `write`; else say what that is."""
    if not left:
        bench.step("anything left changed by an earlier run", "nothing")
        return
    if not bench.write:
        for ref in left:
            bench.step(f"would put back {ref}", "shadowed", "run with --write to put it back")
        return
    await bench.start_executor()
    results = await bench.executor.restore("an earlier run of the rig left it changed", left)
    for ref in left:
        result = results.get(ref)
        if result is not None:
            bench.put_back_step(ref, result)
