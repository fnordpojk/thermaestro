"""The plant's room sensors and the house's power meter, as a plugin: what Home Assistant
or MQTT sensors give the core in a real home. Many Nibe pumps have no room sensor of
their own, so indoor temperatures reach the core this way."""

import asyncio
from typing import Protocol

from thermaestro import clock
from thermaestro.cap import Message, Send
from thermaestro.cap.messages import (
    Describe,
    Described,
    Error,
    Health,
    Read,
    Subscribe,
    Update,
    Values,
)
from thermaestro.cap.model import Delivery, Envelope, Node, Point, Presence

PUSH_S = 60.0


class Sensed(Protocol):
    @property
    def house_kw(self) -> float: ...

    def indoor(self, number: int = 1) -> float: ...

    @property
    def systems(self) -> int: ...


class PlantSensors:
    name = "plant_sensors"
    version = "0.1.0"
    features: tuple[str, ...] = ("subscribe",)

    def __init__(self, plant: Sensed) -> None:
        self.plant = plant

    def _points(self) -> dict[str, float]:
        out = {
            f"room.cs{i}/temperature": round(self.plant.indoor(i), 2)
            for i in range(1, self.plant.systems + 1)
        }
        out["meter/grid.import.power"] = round(self.plant.house_kw, 3)
        return out

    def _envelope(self, path: str) -> Envelope:
        values = self._points()
        now = clock.now()
        if path not in values:
            return Envelope(
                point=path,
                value=None,
                t_observed=None,
                t_received=now,
                quality="unknown",
                source="measured",
                why="no such point",
            )
        unit = "kW" if path.startswith("meter/") else "degC"
        return Envelope(
            point=path,
            value=values[path],
            unit=unit,
            t_observed=now,
            t_received=now,
            quality="good",
            source="measured",
        )

    async def handle(self, request: Message, send: Send) -> None:
        match request:
            case Describe():
                nodes = [
                    Node(path=f"room.cs{i}", kind="room", presence=Presence(how="configured"))
                    for i in range(1, self.plant.systems + 1)
                ]
                nodes.append(Node(path="meter", kind="meter", presence=Presence(how="configured")))
                delivery = Delivery(how="pushed", interval_s=PUSH_S)
                points = tuple(
                    Point(
                        path=p, unit="kW" if p.startswith("meter/") else "degC", delivery=delivery
                    )
                    for p in self._points()
                )
                await send(Described(id=request.id, nodes=tuple(nodes), points=points))
            case Read():
                await send(Values(id=request.id, values=tuple(map(self._envelope, request.points))))
            case Subscribe():
                while True:
                    await send(
                        Update(id=request.id, values=tuple(map(self._envelope, request.points)))
                    )
                    await asyncio.sleep(PUSH_S)
            case _:
                await send(Error(id=getattr(request, "id", None), code="unsupported"))

    async def events(self, send: Send) -> None:
        await send(Health(t=clock.now(), state="up"))
