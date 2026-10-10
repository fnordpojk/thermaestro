"""The pages for control: what the household wants (the Intents page, and quick requests
on the overview), each lever's mode, its competing features confirmed off, a change made
elsewhere kept; and setup's answers about the house. Each form posts to the same
operations as the API."""

import re
from datetime import datetime, tzinfo
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, Response
from starlette.datastructures import FormData

from ..auth import AccountError
from ..planner.rules import EDGE_MARGIN
from .app import action, caller, local_path, services
from .operations import Caller
from .pages import _message, back, render

router = APIRouter(default_response_class=HTMLResponse)

Logged = Annotated[Caller, Depends(caller)]
Text = Annotated[str, Form()]


@router.post("/levers/{ref:path}/mode")
@action("lever.mode")
async def set_mode(request: Request, who: Logged, ref: str, mode: Text) -> Response:
    from .setup_pages import attempt

    work = services(request).set_lever_mode(who, ref, mode)
    return await attempt(request, who, work, "control")


@router.post("/levers/{ref:path}/confirmed-off")
@action("lever.confirm_off")
async def confirm_off(request: Request, who: Logged, ref: str) -> Response:
    from .setup_pages import attempt

    features = [f for f in (await request.form()).getlist("features") if isinstance(f, str)]
    work = services(request).confirm_lever_off(who, ref, features)
    return await attempt(request, who, work, "control")


@router.post("/levers/{ref:path}/accept-drift")
@action("lever.accept_drift")
async def accept_drift(request: Request, who: Logged, ref: str) -> Response:
    from .setup_pages import attempt

    return await attempt(request, who, services(request).accept_drift(who, ref), "control")


@router.post("/settings/home")
@action("home.write")
async def set_home(request: Request, who: Logged) -> Response:
    """Setup's answers about the house. A climate system's emitter comes as
    `emitter:<its node>`."""
    from .setup_pages import attempt

    form = await request.form()
    emitters = {
        key.removeprefix("emitter:"): value
        for key, value in form.items()
        if key.startswith("emitter:") and isinstance(value, str)
    }
    body: dict[str, object] = {"emitters": emitters}
    for name in ("house", "water", "past_deadline"):
        value = form.get(name)
        if isinstance(value, str) and value:
            body[name] = value
    holidays = form.get("holidays")
    body["holidays"] = (
        holidays.strip().upper() if isinstance(holidays, str) and holidays.strip() else None
    )
    holidays_as = form.get("holidays_as")
    body["holidays_as"] = (
        int(holidays_as) if isinstance(holidays_as, str) and holidays_as.isdigit() else None
    )
    return await attempt(
        request, who, services(request).set_home(who, body), "house", "/setup/house#home"
    )


# --- what the household wants -------------------------------------------------------------

PATTERN_ROWS = 4
DEADLINE_ROWS = 3
SPAN_ROWS = 2


def _text(form: FormData, name: str) -> str:
    value = form.get(name)
    return value.strip() if isinstance(value, str) else ""


def _number(form: FormData, name: str) -> float:
    try:
        return float(_text(form, name).replace(",", "."))
    except ValueError:
        raise AccountError(f"{name} is a number") from None


def _moment(text: str, zone: tzinfo) -> str:
    """A time from a form, in the house's time zone unless it says its own."""
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        raise AccountError(f"{text!r} is not a time") from None
    return (moment if moment.tzinfo else moment.replace(tzinfo=zone)).isoformat()


def _days(form: FormData, name: str) -> list[int]:
    return sorted({int(d) for d in form.getlist(name) if isinstance(d, str) and d.isdigit()})


def _rows(form: FormData, prefix: str) -> list[int]:
    """The rows a form has of a repeated group (`p0.level`, `p1.level` …), however many."""
    found = (re.match(rf"{re.escape(prefix)}(\d+)\.", key) for key in form)
    return sorted({int(m.group(1)) for m in found if m})


def request_from_form(form: FormData, zone: tzinfo) -> dict[str, Any]:
    """A request from one of the Intents page's forms: only the fields it filled in."""
    body: dict[str, Any] = {"kind": _text(form, "kind")}
    for name in ("scope", "strength", "hot_water", "policy", "level"):
        if _text(form, name):
            body[name] = _text(form, name)
    for name in ("offset", "at_least", "temp", "kw"):
        if _text(form, name):
            body[name] = _number(form, name)
    if _text(form, "slider"):
        body["slider"] = _number(form, "slider") / 100
    if _text(form, "steps"):
        body["steps"] = int(_number(form, "steps"))
    if _text(form, "no_sensor"):
        body["no_sensor"] = True
    for name in ("until", "by"):
        if _text(form, name):
            body[name] = _moment(_text(form, name), zone)
    levels = {
        key.removeprefix("levels:"): value
        for key, value in form.items()
        if key.startswith("levels:") and isinstance(value, str) and value
    }
    if levels:
        body["levels"] = levels
    ranking = [_text(form, f"rank{i}") for i in range(1, 6)]
    if any(ranking):
        body["ranking"] = ranking
    pattern = [
        {
            "level": _text(form, f"p{i}.level"),
            "days": _days(form, f"p{i}.days"),
            "start": _text(form, f"p{i}.start") or None,
            "end": _text(form, f"p{i}.end") or None,
        }
        for i in _rows(form, "p")
        if _text(form, f"p{i}.level")
    ]
    if pattern:
        body["pattern"] = pattern
    deadlines = []
    for i in _rows(form, "d"):
        if not _text(form, f"d{i}.by"):
            continue
        due: dict[str, Any] = {"by": _text(form, f"d{i}.by"), "days": _days(form, f"d{i}.days")}
        if _text(form, f"d{i}.level"):
            due["level"] = _text(form, f"d{i}.level")
        elif _text(form, f"d{i}.temp"):
            due["temp"] = _number(form, f"d{i}.temp")
        deadlines.append(due)
    if deadlines:
        body["deadlines"] = deadlines
    spans = [
        {
            "days": _days(form, f"s{i}.days"),
            "start": _text(form, f"s{i}.start") or None,
            "end": _text(form, f"s{i}.end") or None,
        }
        for i in _rows(form, "s")
        if _text(form, f"s{i}.start") or _text(form, f"s{i}.end") or _days(form, f"s{i}.days")
    ]
    if spans:
        body["spans"] = spans
    if _text(form, "season_from") and _text(form, "season_to"):
        body["season"] = [_text(form, "season_from"), _text(form, "season_to")]
    return body


async def _intents_page(
    request: Request, who: Caller, status_code: int = 200, **extra: Any
) -> Response:
    from .labels import KINDS, POLICIES, RANKS, STATES

    s = services(request)
    force = await s.in_force(who)
    reach = {
        b["scope"]: max(0.0, (b["high"] - b["low"]) / 2 - EDGE_MARGIN)
        for b in force["bounds"]
        if b["target"] == "room_temp" and b["low"] is not None and b["high"] is not None
    }
    return render(
        request,
        "intents.html",
        who,
        status_code=status_code,
        views=await s.intent_views(who),
        force=force,
        reach=reach,
        levels=await s.level_list(who),
        scopes=s.intent_scopes(who),
        kinds=KINDS,
        ranks=RANKS,
        policies=POLICIES,
        states=STATES,
        rows={"pattern": PATTERN_ROWS, "deadlines": DEADLINE_ROWS, "spans": SPAN_ROWS},
        **{"answer": None, "error": None, **extra},
    )


@router.get("/plan")
async def plan_page(request: Request, who: Logged) -> Response:
    from .labels import RANKS

    s = services(request)
    plan = s.plan(who)
    return render(
        request,
        "plan.html",
        who,
        plan=plan,
        decided={d["lever"]: d for d in plan["decisions"]},
        levers=(
            await s.levers(who)
            if who.principal.allows("settings.read")
            else [{"lever": d["lever"], "mode": "", "unavailable": None} for d in plan["decisions"]]
        ),
        changes=await s.recent_acts(who),
        scopes=s.intent_scopes(who),
        ranks=RANKS,
    )


@router.get("/intents")
async def intents_page(request: Request, who: Logged) -> Response:
    return await _intents_page(request, who)


@router.post("/intents")
@action("intent.ask")
async def ask(request: Request, who: Logged) -> Response:
    """Ask from a form. The answer is shown on the Intents page, or, asked from the
    conversation (htmx), as its next line: what it means, or why it was refused."""
    s = services(request)
    form = await request.form()
    chat = "hx-request" in request.headers
    try:
        answer = await s.ask(who, request_from_form(form, s.zone))
    except AccountError as e:
        if chat:
            return _said(request, who, error=_message(e))
        return await _intents_page(request, who, 400, error=_message(e))
    if chat:
        response = _said(request, who, answer=answer)
        if answer["accepted"]:
            response.headers["HX-Trigger"] = "page-changed"  # the list of what applies
        return response
    status = 200 if answer["accepted"] else 400
    return await _intents_page(request, who, status, answer=answer)


@router.post("/intents/{id}/edit")
@action("intent.edit")
async def edit(request: Request, who: Logged, id: str) -> Response:
    """Change what an intent holds, from the form under it in "Asked for"."""
    s = services(request)
    try:
        answer = await s.edit_intent(who, id, request_from_form(await request.form(), s.zone))
    except AccountError as e:
        return await _intents_page(request, who, 400, error=_message(e))
    return await _intents_page(request, who, 200 if answer["accepted"] else 400, answer=answer)


@router.post("/ask")
@action("intent.understand")
async def understand(request: Request, who: Logged, text: Text = "", quiet: Text = "") -> Response:
    """What was typed, read as a request, shown as a form to check before it is asked; or,
    not understood, what can be said. Asked from the conversation (htmx), its next lines;
    else a page of its own."""
    s = services(request)
    found = await s.understand(who, text)
    context: dict[str, Any] = {
        "said": "" if quiet else text.strip(),
        "found": found,
        "scopes": s.intent_scopes(who),
        "levels": await s.level_list(who),
    }
    if "hx-request" in request.headers:
        return render(request, "ask-reply.html", who, **context)
    return render(request, "ask.html", who, **context)


def _said(request: Request, who: Caller, **context: Any) -> Response:
    """The conversation's answer to asking: accepted with what it means, or refused."""
    return render(request, "ask-answer.html", who, **{"answer": None, "error": None, **context})


@router.post("/intents/{id}/end")
@action("intent.end")
async def end(request: Request, who: Logged, id: str, next: Text = "/intents") -> Response:
    try:
        await services(request).end_intent(who, id)
    except AccountError as e:
        return await _intents_page(request, who, 400, error=_message(e))
    return back(local_path(next, "/intents"))


@router.post("/intents/{id}/confirm")
@action("intent.confirm")
async def confirm(request: Request, who: Logged, id: str) -> Response:
    try:
        await services(request).confirm_intent(who, id)
    except AccountError as e:
        return await _intents_page(request, who, 400, error=_message(e))
    return back("/intents")


@router.post("/levels/{id}")
@action("level.write")
async def put_level(request: Request, who: Logged, id: str) -> Response:
    form = await request.form()
    body: dict[str, Any] = {"name": _text(form, "name"), "scope": _text(form, "scope")}
    if id == "new":  # an id from its name, not taken yet
        known = await services(request).level_list(who)
        stem = re.sub(r"[^a-z0-9]+", "-", body["name"].lower()).strip("-")[:56] or "level"
        id, n = stem, 2
        while id in known:
            id, n = f"{stem}-{n}", n + 1
    try:
        for name in ("low", "high", "top"):
            if _text(form, name):
                body[name] = _number(form, name)
        await services(request).put_level(who, id, body)
    except AccountError as e:
        return await _intents_page(request, who, 400, error=_message(e))
    return back("/intents#levels")


@router.post("/levels/{id}/delete")
@action("level.delete")
async def delete_level(request: Request, who: Logged, id: str) -> Response:
    try:
        await services(request).delete_level(who, id)
    except AccountError as e:
        return await _intents_page(request, who, 400, error=_message(e))
    return back("/intents#levels")
