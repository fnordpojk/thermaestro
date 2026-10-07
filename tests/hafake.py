"""A small stand-in for Home Assistant's WebSocket API: login, `get_states`, the three
registries, `subscribe_entities` with the compressed state format,
`weather/subscribe_forecast`, and ways to send changes. Only what the plugin uses."""

import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from aiohttp import WSMsgType, web
from aiohttp.test_utils import TestServer

TOKEN = "good-token"


class FakeHomeAssistant:
    def __init__(self) -> None:
        self.states: dict[str, dict[str, Any]] = {}
        self.areas = {"living": "Living room", "bed": "Bedroom"}
        self.entity_areas: dict[str, str] = {}
        self.subscribers: list[tuple[web.WebSocketResponse, int, list[str]]] = []
        self.subscribed: list[list[str]] = []
        self.forecasts: dict[str, list[dict[str, Any]]] = {}
        """An hourly forecast per weather entity, in the entity's units."""
        self.forecast_subscribers: list[tuple[web.WebSocketResponse, int, str]] = []

    async def new_forecast(self, entity: str, forecast: list[dict[str, Any]]) -> None:
        self.forecasts[entity] = forecast
        for ws, id, subscribed in self.forecast_subscribers:
            if subscribed == entity and not ws.closed:
                await ws.send_json(
                    {"id": id, "type": "event", "event": {"type": "hourly", "forecast": forecast}}
                )

    def set(self, entity: str, state: str, **attributes: Any) -> None:
        self.states[entity] = {"s": state, "a": attributes, "lc": time.time()}

    async def change(self, entity: str, state: str | None = None, **attributes: Any) -> None:
        current = self.states[entity]
        plus: dict[str, Any] = {"lc": time.time()}
        if state is not None:
            current["s"] = plus["s"] = state
        if attributes:
            current["a"].update(attributes)
            plus["a"] = attributes
        for ws, id, entities in self.subscribers:
            if entity in entities and not ws.closed:
                await ws.send_json(
                    {"id": id, "type": "event", "event": {"c": {entity: {"+": plus}}}}
                )

    async def handler(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        await ws.send_json({"type": "auth_required", "ha_version": "2026.10.0"})
        auth = await ws.receive_json()
        if auth.get("access_token") != TOKEN:
            await ws.send_json({"type": "auth_invalid", "message": "Invalid access token"})
            await ws.close()
            return ws
        await ws.send_json({"type": "auth_ok", "ha_version": "2026.10.0"})
        async for message in ws:
            if message.type != WSMsgType.TEXT:
                continue
            command = message.json()
            await self._command(ws, command)
        return ws

    async def _command(self, ws: web.WebSocketResponse, command: dict[str, Any]) -> None:
        id, kind = command["id"], command["type"]
        if kind == "get_states":
            result: Any = [
                {"entity_id": e, "state": s["s"], "attributes": s["a"]}
                for e, s in self.states.items()
            ]
        elif kind == "config/entity_registry/list":
            result = [
                {"entity_id": e, "area_id": self.entity_areas.get(e), "device_id": None}
                for e in self.states
            ]
        elif kind == "config/device_registry/list":
            result = []
        elif kind == "config/area_registry/list":
            result = [{"area_id": a, "name": n} for a, n in self.areas.items()]
        elif kind == "subscribe_entities":
            entities = command.get("entity_ids") or []
            self.subscribed.append(entities)
            await ws.send_json({"id": id, "type": "result", "success": True, "result": None})
            self.subscribers.append((ws, id, entities))
            added = {e: s for e, s in self.states.items() if e in entities}
            await ws.send_json({"id": id, "type": "event", "event": {"a": added}})
            return
        elif kind == "weather/subscribe_forecast":
            entity = command["entity_id"]
            if entity not in self.forecasts:
                await ws.send_json(
                    {
                        "id": id,
                        "type": "result",
                        "success": False,
                        "error": {
                            "code": "invalid_entity_id",
                            "message": "Weather entity not found",
                        },
                    }
                )
                return
            await ws.send_json({"id": id, "type": "result", "success": True, "result": None})
            self.forecast_subscribers.append((ws, id, entity))
            await ws.send_json(
                {
                    "id": id,
                    "type": "event",
                    "event": {"type": command["forecast_type"], "forecast": self.forecasts[entity]},
                }
            )
            return
        else:
            await ws.send_json(
                {"id": id, "type": "result", "success": False, "error": {"message": "unknown"}}
            )
            return
        await ws.send_json({"id": id, "type": "result", "success": True, "result": result})


@asynccontextmanager
async def running(fake: FakeHomeAssistant) -> AsyncIterator[str]:
    """Serve the fake; yields its URL."""
    app = web.Application()
    app.router.add_get("/api/websocket", fake.handler)
    server = TestServer(app, host="127.0.0.1")
    await server.start_server()
    try:
        yield f"http://127.0.0.1:{server.port}"
    finally:
        for ws, _, _ in fake.subscribers:
            await ws.close()
        await server.close()
