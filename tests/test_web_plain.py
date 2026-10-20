"""Plain HTTP while HTTPS is on: what it serves, and where it sends the rest."""

import httpx
from fastapi import FastAPI

from thermaestro.store.startup import Startup
from thermaestro.web.server import to_https


def app() -> FastAPI:
    inner = FastAPI()

    @inner.get("/{path:path}")
    async def anything(path: str) -> dict[str, str]:
        return {"served": path}

    return inner


async def get(client: tuple[str, int], url: str) -> httpx.Response:
    transport = httpx.ASGITransport(app=to_https(app(), 8443), client=client)
    async with httpx.AsyncClient(transport=transport) as http:
        return await http.get(url)


async def test_from_the_network_a_page_is_sent_to_https() -> None:
    answer = await get(("192.0.2.5", 50000), "http://thermaestro.local:8080/login?next=/plan")
    assert answer.status_code == 308
    assert answer.headers["location"] == "https://thermaestro.local:8443/login?next=/plan"
    by_address = await get(("192.0.2.5", 50000), "http://192.168.23.16:8080/api/v1/status")
    assert by_address.headers["location"] == "https://192.168.23.16:8443/api/v1/status"


async def test_liveness_readiness_and_this_machine_are_served() -> None:
    for path in ("/health", "/ready"):
        answer = await get(("192.0.2.5", 50000), f"http://thermaestro.local:8080{path}")
        assert answer.status_code == 200
    for here in ("127.0.0.1", "::1"):
        answer = await get((here, 50000), "http://127.0.0.1:8080/api/v1/status")
        assert answer.json() == {"served": "api/v1/status"}


def test_plain_http_is_redirected_unless_said_otherwise() -> None:
    assert Startup().web.http == "redirect"
    assert Startup.model_validate({"web": {"http": "serve"}}).web.http == "serve"
