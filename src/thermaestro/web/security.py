"""The browser side's protections: response headers, the CSRF token, the Origin check.

CSRF uses a signed double-submit token bound to the session: an HMAC of the session's
hash under a key only the server has. Before login there is no session, so the login and
setup forms get the same kind of token bound to a random pre-session cookie. Every
unsafe request carrying cookies must also come from this site's own origin. Requests
with a bearer token carry no cookies, so they need neither.
"""

import hashlib
import hmac
import secrets
from collections.abc import Awaitable, Callable, MutableMapping
from typing import Any
from urllib.parse import urlsplit

from starlette.requests import Request

SESSION_COOKIE = "thermaestro_session"
PRE_SESSION_COOKIE = "thermaestro_pre"
CSRF_FIELD = "csrf"
CSRF_HEADER = "x-csrf-token"
BACKGROUND_HEADER = "x-thermaestro-background"
"""Sent by a page refreshing what it shows by itself: not someone using the session."""
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

CSP = (
    "default-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'self'; "
    "form-action 'self'"
)
HEADERS: list[tuple[bytes, bytes]] = [
    (b"content-security-policy", CSP.encode()),
    (b"x-content-type-options", b"nosniff"),
    (b"referrer-policy", b"strict-origin-when-cross-origin"),
    (
        b"permissions-policy",
        b"accelerometer=(), camera=(), geolocation=(), gyroscope=(), magnetometer=(), "
        b"microphone=(), payment=(), usb=(), interest-cohort=()",
    ),
    (b"cross-origin-opener-policy", b"same-origin"),
    (b"x-frame-options", b"SAMEORIGIN"),
]

Scope = MutableMapping[str, Any]
Message = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]
App = Callable[[Scope, Receive, Send], Awaitable[None]]


class SecurityHeaders:
    """Adds the headers to every HTTP response, error pages included."""

    def __init__(self, app: App) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                names = {name.lower() for name, _ in message.get("headers", [])}
                extra = [(n, v) for n, v in HEADERS if n not in names]
                message["headers"] = [*message.get("headers", []), *extra]
            await send(message)

        await self.app(scope, receive, with_headers)


def csrf_token(key: bytes, binding: str) -> str:
    """The token for a session (its hash) or a pre-session cookie's value."""
    return hmac.new(key, f"csrf:{binding}".encode(), hashlib.sha256).hexdigest()


def csrf_matches(key: bytes, binding: str, token: str | None) -> bool:
    if not token:
        return False
    return hmac.compare_digest(csrf_token(key, binding).encode(), token.encode())


def new_pre_session() -> str:
    return secrets.token_urlsafe(24)


def same_origin(request: Request) -> bool:
    """Whether an unsafe request came from a page of this site: its Origin, or failing
    that its Referer, names the host the request was sent to."""
    host = request.headers.get("host")
    if not host:
        return False
    origin = request.headers.get("origin")
    if origin is None or origin == "null":
        referer = request.headers.get("referer")
        if not referer:
            return False
        origin = referer
    parts = urlsplit(origin)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return False
    return hmac.compare_digest(parts.netloc.lower().encode(), host.lower().encode())


def client_address(request: Request) -> str:
    return request.client.host if request.client else "unknown"
