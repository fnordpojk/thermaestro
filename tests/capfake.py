"""A small plugin for the conformance tests: one invented heat pump. Run as a script, it
connects to a listening core from its own process, as an out-of-process plugin would.

`flaws` makes it break one rule at a time, to show the suite notices.
"""

import argparse
import asyncio
import sys
from datetime import UTC, datetime
from pathlib import Path

from thermaestro.cap import Message, Send, serve
from thermaestro.cap.messages import (
    Act,
    Describe,
    Described,
    Error,
    Fate,
    Health,
    Read,
    Subscribe,
    Update,
    Values,
)
from thermaestro.cap.model import (
    Delivery,
    Envelope,
    Identity,
    Implementation,
    Knowledge,
    Lever,
    Node,
    Param,
    Point,
    Presence,
    Range,
    Verify,
)
from thermaestro.cap.sockets import connect_tcp, connect_unix

PUSH_S = 0.05

NODES = (
    Node(
        path="hp1",
        kind="unit",
        presence=Presence(how="configured"),
        identity=Identity(vendor="Fake", model="FP-1", firmware="1.0"),
    ),
    Node(path="hp1/cs1", kind="climate_system", presence=Presence(how="assumed")),
    Node(path="hp1/dhw", kind="dhw_tank", presence=Presence(how="assumed")),
)

POLLED = Delivery(how="polled", cost_s=1.0)

POINTS = (
    Point(path="hp1/outdoor.temp", unit="degC", delivery=Delivery(how="pushed", interval_s=PUSH_S)),
    Point(path="hp1/dhw/temp.top", unit="degC", delivery=POLLED),
    Point(path="hp1/cs1/supply.temp", unit="degC", delivery=POLLED),
    Point(path="hp1/cs1/x.fake.offset", unit=None, delivery=POLLED),
    Point(path="hp1/x.fake.prio", unit=None, delivery=POLLED),
)

LEVERS = (
    Lever(
        path="hp1/cs1/heating.offset",
        kind="setting",
        params={
            "value": Param(
                type="number",
                range=Knowledge(value=Range(min=-10, max=10, step=1), known="documented"),
            )
        },
        works=Knowledge(value=True, known="documented"),
        verify=Verify(kind="readback", point="hp1/cs1/x.fake.offset"),
        touches=("x.fake.offset",),
    ),
    Lever(
        path="hp1/dhw/block",
        kind="hold",
        implementation=Implementation(
            kind="emulated", how="a low start temperature", side_effects=("a floor",)
        ),
        verify=Verify(kind="effect", point="hp1/dhw/temp.top", expectation="no charge"),
        touches=("x.fake.start",),
    ),
)

READINGS: dict[str, float | int | None] = {
    "hp1/outdoor.temp": 4.5,
    "hp1/dhw/temp.top": 51.0,
    "hp1/cs1/supply.temp": None,  # the sensor isn't connected
    "hp1/cs1/x.fake.offset": 0,
    "hp1/x.fake.prio": 30,
}


def now() -> datetime:
    return datetime.now(UTC)


class FakePump:
    name = "fake"
    version = "0.1.0"
    features: tuple[str, ...] = ("subscribe",)

    def __init__(self, flaws: frozenset[str] = frozenset()) -> None:
        self.flaws = flaws
        self.acts: list[Act] = []
        self.leaked: list[asyncio.Task[None]] = []

    async def handle(self, request: Message, send: Send) -> None:
        match request:
            case Describe():
                await send(self.describe(request.id))
            case Read():
                await send(Values(id=request.id, values=tuple(map(self.envelope, request.points))))
            case Subscribe() if "keeps-updating" in self.flaws:
                # A task of its own outlives the subscription.
                self.leaked.append(asyncio.create_task(self.updates(request, send)))
            case Subscribe():
                await self.updates(request, send)
            case Act():
                await self.act(request, send)
            case _:
                await send(
                    Error(id=getattr(request, "id", None), code="unsupported", detail="not here")
                )

    async def events(self, send: Send) -> None:
        await send(Health(t=now(), unit="hp1", state="up", last_traffic=now()))

    def describe(self, id: int) -> Described:
        points: tuple[Point, ...] = POINTS
        if "wrong-unit" in self.flaws:
            points = (points[0].model_copy(update={"unit": "K"}), *points[1:])
        if "made-up-name" in self.flaws:
            points = (*points, Point(path="hp1/flow.temp", unit="degC", delivery=POLLED))
        return Described(id=id, nodes=NODES, points=points, levers=LEVERS)

    def envelope(self, point: str) -> Envelope:
        if point not in READINGS:
            good = "unknown-reads-good" in self.flaws
            return Envelope(
                point=point,
                value=0 if good else None,
                t_observed=now() if good else None,
                t_received=now(),
                quality="good" if good else "unknown",
                source="measured",
                why=None if good else "no such point",
            )
        value = READINGS[point]
        unit = next(p.unit for p in POINTS if p.path == point)
        return Envelope(
            point=point,
            value=value,
            unit=unit,
            t_observed=now(),
            t_received=now(),
            quality="good" if value is not None else "not_connected",
            source="measured",
            why=None if value is not None else "sensor not connected",
        )

    async def updates(self, request: Subscribe, send: Send) -> None:
        while True:
            await asyncio.sleep(max(PUSH_S, request.min_interval_s))
            await send(Update(id=request.id, values=tuple(map(self.envelope, request.points))))

    async def act(self, request: Act, send: Send) -> None:
        self.acts.append(request)
        known = any(lv.path == request.lever for lv in LEVERS)
        if not known and "act-accepts-anything" not in self.flaws:
            await send(Fate(id=request.id, stage="dropped", t=now(), detail="no such lever"))
            return
        await send(Fate(id=request.id, stage="queued", t=now()))
        await send(Fate(id=request.id, stage="sent", t=now()))
        await send(Fate(id=request.id, stage="device_accepted", t=now(), detail="ack"))


async def main() -> None:
    parser = argparse.ArgumentParser()
    where = parser.add_mutually_exclusive_group(required=True)
    where.add_argument("--unix")
    where.add_argument("--tcp")
    parser.add_argument("--flaw", action="append", default=[])
    args = parser.parse_args()
    if args.unix:
        endpoint = await connect_unix(Path(args.unix))
    else:
        endpoint = await connect_tcp(Path(args.tcp))
    try:
        await serve(endpoint, FakePump(frozenset(args.flaw)))
    finally:
        await endpoint.close()


if __name__ == "__main__":
    asyncio.run(main())
    sys.exit(0)
