"""An invented device with one lever of each kind, for the write path's tests.

Its registers are plain numbers. Switches make it misbehave one way at a time: a write
accepted but not kept, a refused one, a slow one, a change made by someone else.
"""

import asyncio
from datetime import UTC, datetime

from thermaestro.cap import Message, Send
from thermaestro.cap.messages import (
    Act,
    Describe,
    Described,
    Error,
    Fate,
    ForeignWrite,
    Health,
    Read,
    Subscribe,
    Update,
    Values,
    Write,
)
from thermaestro.cap.model import (
    CompetingFeature,
    Delivery,
    Envelope,
    Identity,
    Knowledge,
    Lever,
    Node,
    Param,
    Persistence,
    Point,
    Presence,
    Range,
    Verify,
)

PUSH_S = 0.05
DOCUMENTED = {"known": "documented", "basis": "the fake's manual"}


def number(low: float, high: float) -> Param:
    return Param(
        type="number", range=Knowledge(value=Range(min=low, max=high, step=1), **DOCUMENTED)
    )


WORKS = Knowledge[bool](value=True, known="documented", basis="the fake's manual")

LEVERS = (
    Lever(
        path="hp1/offset",
        kind="setting",
        params={"value": number(-10, 10)},
        works=WORKS,
        verify=Verify(kind="readback", point="hp1/x.fake.offset"),
        touches=("x.fake.offset",),
    ),
    Lever(
        path="hp1/twin",
        kind="setting",
        params={"value": number(-10, 10)},
        works=WORKS,
        verify=Verify(kind="readback", point="hp1/x.fake.offset"),
        touches=("x.fake.offset",),
    ),
    Lever(
        path="hp1/curve",
        kind="setting",
        params={"value": number(0, 15)},
        works=WORKS,
        preconditions=Knowledge(value=("hp1/x.fake.room_control == 0",), **DOCUMENTED),
        verify=Verify(kind="readback", point="hp1/x.fake.curve"),
        touches=("x.fake.curve",),
    ),
    Lever(
        path="hp1/mode",
        kind="setting",
        params={
            "value": Param(type="enum", enum=Knowledge(value={"eco": 0, "normal": 1}, **DOCUMENTED))
        },
        works=WORKS,
        verify=Verify(kind="readback", point="hp1/x.fake.mode"),
        competing_features=(CompetingFeature(name="the schedule"),),
        touches=("x.fake.mode",),
    ),
    Lever(
        path="hp1/untested",
        kind="setting",
        params={"value": number(0, 5)},
        verify=Verify(kind="readback", point="hp1/x.fake.untested"),
        touches=("x.fake.untested",),
    ),
    Lever(
        path="hp1/block",
        kind="hold",
        works=WORKS,
        verify=Verify(kind="effect", point="hp1/x.fake.start", expectation="no charge"),
        touches=("x.fake.start",),
    ),
    Lever(
        path="hp1/boost",
        kind="trigger",
        works=WORKS,
        verify=Verify(kind="none"),
        touches=("x.fake.boost",),
    ),
    Lever(
        path="hp1/alarm.reset",
        kind="trigger",
        works=WORKS,
        verify=Verify(kind="none"),
        touches=("x.fake.alarm",),
    ),
    Lever(
        path="hp1/leased",
        kind="setting",
        params={"value": number(0, 100)},
        works=WORKS,
        persistence=Knowledge(value=Persistence(kind="leased", period_s=0.4), **DOCUMENTED),
        verify=Verify(kind="readback", point="hp1/x.fake.leased"),
        touches=("x.fake.leased",),
    ),
)

REGISTERS = {
    "x.fake.offset": -4,
    "x.fake.curve": 7,
    "x.fake.mode": 1,
    "x.fake.untested": 2,
    "x.fake.start": 45,
    "x.fake.room_control": 0,
    "x.fake.leased": 0,
}


def now() -> datetime:
    return datetime.now(UTC)


class LeverDevice:
    name = "leverfake"
    version = "0.1.0"
    features: tuple[str, ...] = ("subscribe", "write")

    def __init__(self) -> None:
        self.registers = dict(REGISTERS)
        self.acts: list[Act] = []
        self.writes: list[Write] = []
        self.not_kept: set[str] = set()
        """Lever paths whose writes are accepted and then dropped."""
        self.refused: set[str] = set()
        self.delay_s = 0.0
        self.held: set[str] = set()
        self._send: Send | None = None

    async def handle(self, request: Message, send: Send) -> None:
        match request:
            case Describe():
                points = tuple(
                    Point(path=f"hp1/{r}", delivery=Delivery(how="pushed", interval_s=PUSH_S))
                    for r in REGISTERS
                )
                nodes = (
                    Node(
                        path="hp1",
                        kind="unit",
                        presence=Presence(how="configured"),
                        identity=Identity(vendor="Fake", model="LF-1"),
                    ),
                )
                await send(Described(id=request.id, nodes=nodes, points=points, levers=LEVERS))
            case Read():
                await send(Values(id=request.id, values=tuple(map(self.envelope, request.points))))
            case Subscribe():
                while True:
                    await asyncio.sleep(PUSH_S)
                    await send(
                        Update(id=request.id, values=tuple(map(self.envelope, request.points)))
                    )
            case Act():
                await self.act(request, send)
            case Write():
                await self.write(request, send)
            case _:
                await send(Error(id=getattr(request, "id", None), code="unsupported"))

    async def events(self, send: Send) -> None:
        self._send = send
        await send(Health(t=now(), unit="hp1", state="up", last_traffic=now()))

    def envelope(self, point: str) -> Envelope:
        register = point.removeprefix("hp1/")
        if register not in self.registers:
            return Envelope(
                point=point,
                value=None,
                t_observed=None,
                t_received=now(),
                quality="unknown",
                source="measured",
            )
        return Envelope(
            point=point,
            value=self.registers[register],
            t_observed=now(),
            t_received=now(),
            quality="good",
            source="measured",
        )

    async def act(self, request: Act, send: Send) -> None:
        self.acts.append(request)
        lever = next((lv for lv in LEVERS if lv.path == request.lever), None)
        if lever is None:
            await send(Fate(id=request.id, stage="dropped", t=now(), detail="no such lever"))
            return
        await send(Fate(id=request.id, stage="queued", t=now()))
        await asyncio.sleep(self.delay_s)
        if request.lever in self.refused:
            await send(Fate(id=request.id, stage="device_refused", t=now()))
            return
        await send(Fate(id=request.id, stage="device_accepted", t=now()))
        if request.lever in self.not_kept:
            return
        register = lever.touches[0]
        if request.op == "set":
            value = request.params["value"]
            enum = lever.params["value"].enum.value
            self.registers[register] = enum[str(value)] if enum else value  # type: ignore[assignment]
        elif request.op == "engage":
            self.held.add(request.lever)
            self.registers[register] = 25
        elif request.op == "release":
            self.held.discard(request.lever)
            self.registers[register] = REGISTERS[register]

    async def write(self, request: Write, send: Send) -> None:
        """A person's change of one of its registers, by its point."""
        self.writes.append(request)
        register = request.point.removeprefix("hp1/")
        if register not in self.registers:
            await send(Fate(id=request.id, stage="dropped", t=now(), detail="no such register"))
            return
        await send(Fate(id=request.id, stage="device_accepted", t=now()))
        self.registers[register] = request.value  # type: ignore[assignment]

    async def redescribe(self, path: str) -> None:
        """Describe a lever anew, as a plugin does when what a hold acts on has moved."""
        lever = next(lv for lv in LEVERS if lv.path == path)
        if self._send is not None:
            await self._send(Described(complete=False, levers=(lever,)))

    async def someone_writes(self, register: str, value: int, *, tell: bool) -> None:
        """Another client, or the pump's menu, changes a register. Over a route that sees
        other writers, the plugin says so."""
        self.registers[register] = value
        if tell and self._send is not None:
            await self._send(ForeignWrite(t=now(), unit="hp1", datapoint=register, value=value))
