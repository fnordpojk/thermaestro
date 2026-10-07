"""Serving the web UI inside the running core: HTTP, and HTTPS with the installation's
own certificate.

uvicorn replaces the SIGINT and SIGTERM handlers while it serves; here it doesn't, so
the core keeps them and stops in its own order. The sockets are bound before uvicorn
starts, so a port in use is an error at start that names the port.
"""

import asyncio
import contextlib
import logging
import secrets
import socket
from collections.abc import Awaitable, Callable, Iterator
from typing import TYPE_CHECKING

import uvicorn

from ..auth import Accounts, SetupCode
from ..store import StoreError
from . import certificate
from .app import create_app
from .operations import Services

if TYPE_CHECKING:
    from ..core.daemon import Core

log = logging.getLogger(__name__)

CSRF_KEY = "web.csrf-key"


class _Server(uvicorn.Server):
    @contextlib.contextmanager
    def capture_signals(self) -> Iterator[None]:
        yield  # the core's handlers stay


def _bind(host: str, port: int) -> socket.socket:
    try:
        return socket.create_server((host, port), reuse_port=False)
    except OSError as e:
        raise StoreError(f"the web UI can't listen on {host} port {port}: {e.strerror}") from e


async def _csrf_key(core: "Core") -> bytes:
    """The key the CSRF tokens are signed with, made once and kept with the secrets, so
    a restart doesn't invalidate open forms."""
    stored = await core.secrets.get(CSRF_KEY)
    if stored is None:
        await core.secrets.set(CSRF_KEY, secrets.token_hex(32))
        stored = await core.secrets.get(CSRF_KEY)
        assert stored is not None  # noqa: S101 - just written
    return bytes.fromhex(stored.get_secret_value())


async def _first_run(accounts: Accounts, setup: SetupCode) -> None:
    """Until an administrator exists, each start makes a new setup code and says where
    it is."""
    if await accounts.has_admin():
        setup.discard()
        return
    code = setup.issue()
    log.warning(
        "No administrator yet. Create one in the web UI with the setup code %s; it works "
        "once, for 60 minutes. `thermaestro setup-code` shows it, or a new one, on the host.",
        code,
    )


async def start(core: "Core") -> Callable[[], Awaitable[None]]:
    """Start the web servers; the function returned stops them."""
    web = core.startup.web
    accounts = Accounts(core.db, core.audit)
    setup = SetupCode(core.layout.setup_code)
    await _first_run(accounts, setup)
    own = certificate.ensure(core.layout.tls) if web.https else None
    services = Services(
        accounts=accounts,
        db=core.db,
        values=core.values,
        host=core.host,
        audit=core.audit,
        secrets=core.secrets,
        setup=setup,
        fingerprint=own.fingerprint if own else None,
        sensors=core.sensors,
        mqtt=core.mqtt,
        series=core.host.series,
        weather=core.weather,
    )
    await services.load_zone()
    app = create_app(services, await _csrf_key(core))

    listeners: list[tuple[socket.socket, uvicorn.Config]] = []
    try:
        common = {
            "lifespan": "off",
            "log_config": None,
            "access_log": False,
            "server_header": False,
            "proxy_headers": False,
            "http": "h11",
            "ws": "none",
            "timeout_graceful_shutdown": 5,
        }
        plain = _bind(web.listen, web.port)
        listeners.append((plain, uvicorn.Config(app, **common)))  # type: ignore[arg-type]
        if own is not None:
            tls = _bind(web.listen, web.https_port)
            config = uvicorn.Config(
                app,
                ssl_certfile=str(own.cert),
                ssl_keyfile=str(own.key),
                **common,  # type: ignore[arg-type]
            )
            listeners.append((tls, config))
    except BaseException:
        for sock, _ in listeners:
            sock.close()
        raise

    servers = [_Server(config) for _, config in listeners]
    tasks = [
        asyncio.create_task(server.serve(sockets=[sock]))
        for server, (sock, _) in zip(servers, listeners, strict=True)
    ]
    while not all(s.started for s in servers):
        failed = [t for t in tasks if t.done()]
        if failed:
            await _stop(servers, tasks, listeners)
            raise StoreError("the web UI didn't start; see the messages before this one")
        await asyncio.sleep(0.02)
    log.info(
        "web UI on http://%s:%d%s",
        web.listen,
        web.port,
        f" and https://{web.listen}:{web.https_port} (SHA-256 {own.fingerprint})" if own else "",
    )

    async def stop() -> None:
        await _stop(servers, tasks, listeners)

    return stop


async def _stop(
    servers: list[_Server],
    tasks: list["asyncio.Task[None]"],
    listeners: list[tuple[socket.socket, uvicorn.Config]],
) -> None:
    for server in servers:
        server.should_exit = True
    results = await asyncio.gather(*tasks, return_exceptions=True)
    for result in results:
        if isinstance(result, BaseException) and not isinstance(result, asyncio.CancelledError):
            log.warning("a web server stopped with %r", result)
    for sock, _ in listeners:
        sock.close()
