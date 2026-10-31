"""Errors in the web UI and the API that nothing else handled: never a bare 500.

Such an error is logged with its traceback and raised as an event under the route it
happened in (`core.failures`), and the answer says something went wrong, with the error's
ID: a page in the site's layout, or `{"error", "id"}` from the API. What the error said is
on the Events page and in the log, not in the answer, which anyone may get. The event clears
the next time that route answers without one.

A page made of parts builds each through `Parts`: one that fails is left out, a default in
its place, and the page says which part couldn't be shown, the rest of it as usual.
"""

import logging
from collections.abc import Awaitable, Mapping
from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse, Response
from starlette.exceptions import HTTPException

from ..auth import AccountError
from ..core.events import mark
from ..core.failures import failures
from . import i18n

log = logging.getLogger(__name__)

SHOWING = mark("Showing a page")
ANSWERING = mark("Answering the API")
WENT_WRONG = mark(
    "Something went wrong on Thermaestro's side (error %(id)s). It is on the Events page, "
    "for those who may see the house's workings, and in Thermaestro's log."
)
PART_MISSING = mark("%(part)s couldn't be shown: something went wrong (error %(id)s).")


def route_of(scope: Mapping[str, Any]) -> str:
    """The route a request went to, as declared (`/points/{instance}/{point}`); its path
    where none matched."""
    route = scope.get("route")
    path = getattr(route, "path", None)
    return path if isinstance(path, str) else str(scope.get("path", ""))


def _api(scope: Mapping[str, Any]) -> bool:
    return str(scope.get("path", "")).startswith("/api/")


def _answered(error: Exception) -> bool:
    """Whether the app answers `error` itself: a refusal, a login or a confirmation asked
    for, a page not found. Those are no failure."""
    from .app import BadRequestOrigin, NotLoggedIn, StepUpRefused

    return isinstance(
        error, AccountError | HTTPException | NotLoggedIn | BadRequestOrigin | StepUpRefused
    )


class Unexpected:
    """The middleware that answers for an error nothing else handled."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        status = 0

        async def sending(message: Any) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, sending)
        except Exception as e:
            route = route_of(scope)
            what = ANSWERING if _api(scope) else SHOWING
            id = failures.failed(f"web:{route}", what, e, where=route, logger=log)
            if status:
                raise  # the answer had begun: it can only be cut off
            await (await _answer(scope, receive, id))(scope, receive, send)
            return
        if status < 500:
            failures.worked(f"web:{route_of(scope)}")


async def _answer(scope: Any, receive: Any, id: str) -> Response:
    text = i18n._(WENT_WRONG, id=id)
    if _api(scope):
        return JSONResponse({"error": "something went wrong", "id": id}, status_code=500)
    from .app import identify
    from .pages import render

    request = Request(scope, receive)
    try:
        who = await identify(request)
    except Exception:  # noqa: BLE001 - shown as logged out, rather than not at all
        who = None
    try:
        return render(request, "error.html", who, status_code=500, error=text)
    except Exception:
        log.exception("the error page couldn't be shown either")
        return Response(text, status_code=500, media_type="text/plain; charset=utf-8")


class Parts:
    """A page's parts, each built on its own. `broken` is what the layout shows."""

    def __init__(self, request: Request) -> None:
        self._route = route_of(request.scope)
        self.broken: list[str] = []
        request.state.parts = self  # for `pages.render`, which shows `broken`

    async def get[T](self, name: str, label: str, build: Awaitable[T], default: T) -> T:
        """`build`'s result; `default` if it failed, with the page saying `label` (a
        marked message naming the part) couldn't be shown."""
        part = f"web:{self._route}#{name}"
        try:
            found = await build
        except Exception as e:
            if _answered(e):
                raise
            id = failures.failed(part, SHOWING, e, where=f"{self._route} ({name})", logger=log)
            self.broken.append(i18n._(PART_MISSING, part=i18n._(label), id=id))
            return default
        failures.worked(part)
        return found
