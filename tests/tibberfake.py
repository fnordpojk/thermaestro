"""A small stand-in for Tibber's GraphQL API: the answer to the price query in the format
Tibber sends (recorded from a real answer, the values invented), and its refusal of an
unknown token."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from aiohttp import web
from aiohttp.test_utils import TestServer

TOKEN = "good-token"
STOCKHOLM = ZoneInfo("Europe/Stockholm")


def day(on: date, base: float = 0.5, zone: ZoneInfo = STOCKHOLM) -> list[dict[str, Any]]:
    """A day's quarters as Tibber lists them: 92, 96 or 100."""
    t = datetime.combine(on, time(0), zone).astimezone(ZoneInfo("UTC"))
    end = datetime.combine(on + timedelta(days=1), time(0), zone).astimezone(ZoneInfo("UTC"))
    out = []
    n = 0
    while t < end:
        energy = round(base + n * 0.001, 4)
        out.append(
            {
                "total": round(1.25 * energy + 0.1248, 4),
                "energy": energy,
                # Tibber writes its local time, with milliseconds.
                "startsAt": t.astimezone(zone).isoformat(timespec="milliseconds"),
                "currency": "SEK",
            }
        )
        t += timedelta(minutes=15)
        n += 1
    return out


class FakeTibber:
    def __init__(self, today: date) -> None:
        self.today = day(today)
        self.tomorrow: list[dict[str, Any]] = []
        self.queries: list[str] = []
        self.homes: list[dict[str, Any]] | None = None

    async def handler(self, request: web.Request) -> web.Response:
        body = await request.json()
        self.queries.append(body["query"])
        if request.headers.get("Authorization") != f"Bearer {TOKEN}":
            return web.json_response(
                {
                    "errors": [
                        {
                            "message": "invalid token",
                            "locations": [{"line": 1, "column": 3}],
                            "path": ["viewer"],
                            "extensions": {"code": "UNAUTHENTICATED"},
                        }
                    ],
                    "data": None,
                }
            )
        homes = self.homes
        if homes is None:
            homes = [
                {
                    "id": "00000000-0000-4000-8000-000000000001",
                    "timeZone": "Europe/Stockholm",
                    "currentSubscription": {
                        "priceInfo": {"today": self.today, "tomorrow": self.tomorrow}
                    },
                }
            ]
        return web.json_response({"data": {"viewer": {"homes": homes}}})


@asynccontextmanager
async def running(fake: FakeTibber) -> AsyncIterator[str]:
    """Serve the fake; yields its URL."""
    app = web.Application()
    app.router.add_post("/v1-beta/gql", fake.handler)
    server = TestServer(app, host="127.0.0.1")
    await server.start_server()
    try:
        yield f"http://127.0.0.1:{server.port}/v1-beta/gql"
    finally:
        await server.close()
