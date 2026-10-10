"""The API for control: intents and levels, setup's answers about the house, the levers
and their modes, and the plan."""

import csv
import io
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel

from .app import action, caller, services
from .operations import Caller

router = APIRouter(prefix="/api/v1")

Logged = Annotated[Caller, Depends(caller)]


class Mode(BaseModel):
    mode: str


class Features(BaseModel):
    features: list[str]


# --- intents ------------------------------------------------------------------------------


@router.get("/intents")
@action("intents.read")
async def intents(request: Request, who: Logged, ended: bool = False) -> list[dict[str, Any]]:
    """The open intents; with `ended`, the finished ones too."""
    found = await services(request).intent_list(who, ended=ended)
    return [i.model_dump(mode="json") for i in found]


@router.post("/intents", status_code=201)
@action("intent.ask")
async def ask(body: dict[str, Any], request: Request, who: Logged) -> dict[str, Any]:
    """Ask for something in household terms, such as `{"kind": "warmer", "scope":
    "pump:hp1/cs1", "offset": 1}` or `{"kind": "bath", "scope": "pump:hp1/dhw",
    "at_least": 50, "by": "2026-10-10T19:30:00+02:00"}`. The answer says whether it was
    accepted, with the intent as kept and what it means."""
    return await services(request).ask(who, body)


class Said(BaseModel):
    text: str


@router.post("/intents/understand")
@action("intent.understand")
async def understand(body: Said, request: Request, who: Logged) -> dict[str, Any]:
    """Read what was typed ("a bath at 19:30", "borta till söndag", "Gäste bis morgen") as a
    request for a while, in English, Swedish or German. Nothing is asked: the answer is
    `{kind, request, missing}`, the request to check and send to `POST /intents` once
    `missing` (`until`, `by`, `levels`) is filled in. `kind` is null when it wasn't
    understood."""
    return await services(request).understand(who, body.text)


@router.put("/intents/{id}")
@action("intent.edit")
async def edit(id: str, body: dict[str, Any], request: Request, who: Logged) -> dict[str, Any]:
    """Change an open intent in place, with the whole request as for asking (its `kind`
    may be left out; it can't change). Answered as asking is."""
    return await services(request).edit_intent(who, id, body)


@router.delete("/intents/{id}")
@action("intent.end")
async def end(id: str, request: Request, who: Logged) -> dict[str, Any]:
    return (await services(request).end_intent(who, id)).model_dump(mode="json")


@router.post("/intents/{id}/confirm")
@action("intent.confirm")
async def confirm(id: str, request: Request, who: Logged) -> dict[str, Any]:
    """A seeded intent becomes the household's own."""
    return (await services(request).confirm_intent(who, id)).model_dump(mode="json")


@router.get("/intents/in-force")
@action("intents_in_force.read")
async def in_force(request: Request, who: Logged) -> dict[str, Any]:
    return await services(request).in_force(who)


@router.get("/levels")
@action("levels.read")
async def levels(request: Request, who: Logged) -> list[dict[str, Any]]:
    found = await services(request).level_list(who)
    return [level.model_dump(mode="json") for level in found.values()]


@router.put("/levels/{id}")
@action("level.write")
async def put_level(id: str, body: dict[str, Any], request: Request, who: Logged) -> dict[str, Any]:
    """`{"name": "Day", "scope": "pump:hp1/cs1", "low": 20.5, "high": 22}`, or a tank's
    `{"name": "Normal", "scope": "pump:hp1/dhw", "top": 50}`."""
    return (await services(request).put_level(who, id, body)).model_dump(mode="json")


@router.delete("/levels/{id}")
@action("level.delete")
async def delete_level(id: str, request: Request, who: Logged) -> dict[str, str]:
    await services(request).delete_level(who, id)
    return {"status": "ok"}


# --- setup's answers about the house ------------------------------------------------------


@router.get("/home")
@action("home.read")
async def home(request: Request, who: Logged) -> dict[str, Any]:
    return (await services(request).home_settings(who)).model_dump(mode="json")


@router.put("/home")
@action("home.write")
async def set_home(body: dict[str, Any], request: Request, who: Logged) -> dict[str, Any]:
    """`{"emitters": {"pump:hp1/cs1": "slab"}, "house": "average", "water": "well",
    "holidays": "SE", "holidays_as": 6, "past_deadline": "keep_heating"}`."""
    return (await services(request).set_home(who, body)).model_dump(mode="json")


# --- levers -------------------------------------------------------------------------------


@router.get("/levers")
@action("levers.read")
async def levers(request: Request, who: Logged) -> list[dict[str, Any]]:
    return await services(request).levers(who)


@router.put("/levers/{ref:path}/mode")
@action("lever.mode")
async def set_mode(ref: str, body: Mode, request: Request, who: Logged) -> dict[str, str]:
    """`{"mode": "shadow"}`: off, shadow or control. Leaving control puts the setting back
    as it was found."""
    await services(request).set_lever_mode(who, ref, body.mode)
    return {"status": "ok"}


@router.put("/levers/{ref:path}/confirmed-off")
@action("lever.confirm_off")
async def confirm_off(ref: str, body: Features, request: Request, who: Logged) -> dict[str, str]:
    """`{"features": ["Smart Price Adaption"]}`: these competing features are switched off."""
    await services(request).confirm_lever_off(who, ref, body.features)
    return {"status": "ok"}


@router.post("/levers/{ref:path}/accept-drift")
@action("lever.accept_drift")
async def accept_drift(ref: str, request: Request, who: Logged) -> dict[str, str]:
    """Keep the change made elsewhere."""
    await services(request).accept_drift(who, ref)
    return {"status": "ok"}


# --- the plan -----------------------------------------------------------------------------


@router.get("/plan")
@action("plan.read")
async def plan(request: Request, who: Logged) -> dict[str, Any]:
    """The planner's last round, each decision with its reason and outcome; and in shadow,
    what it would have done."""
    return services(request).plan(who)


@router.get("/plan/changes")
@action("plan_changes.read")
async def changes(request: Request, who: Logged, hours: float = 24.0) -> list[dict[str, Any]]:
    """Every change asked in the last `hours` (at most a week), newest first: made, shadowed
    or refused, with who asked, why, and what became of it."""
    return await services(request).recent_acts(who, min(max(hours, 0.0), 168.0))


@router.get("/shadow")
@action("shadow.read")
async def shadow(
    request: Request, who: Logged, days: int = 7, lever: str = "", day: str = ""
) -> list[dict[str, Any]]:
    """What shadow would have done, newest first, each with what the device showed then
    (`found`: point → `{value, unit}`): over the last `days` (at most 30), or one `day`
    (`YYYY-MM-DD`, the house's), for one `lever` or all."""
    return await services(request).shadow_log(who, days=days, lever=lever or None, day=day or None)


@router.get("/shadow/series")
@action("shadow.series")
async def shadow_series(request: Request, who: Logged, lever: str, days: int = 7) -> dict[str, Any]:
    """For a chart of a lever in shadow: `pump`, the history of the point it is checked by,
    and `shadow`, what shadow would have had as steps `{t, value}` (a setting's value; a
    hold's `held` or `released`; a trigger's `started`)."""
    return await services(request).shadow_series(who, lever, days)


@router.get("/shadow.csv")
@action("shadow.export")
async def shadow_csv(
    request: Request, who: Logged, days: int = 7, lever: str = "", day: str = ""
) -> Response:
    """The same as `GET /shadow`, as CSV: one row per decision, the time in the house's
    zone, what the device showed as `point=value unit` pairs."""
    s = services(request)
    rows = await s.shadow_log(who, days=days, lever=lever or None, day=day or None)
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(["time", "lever", "asked", "value", "the device showed", "why"])
    for row in rows:
        local = datetime.fromisoformat(row["t"]).astimezone(s.zone)
        shown = "; ".join(
            f"{point}={seen.get('value')}" + (f" {seen['unit']}" if seen.get("unit") else "")
            for point, seen in row["found"].items()
        )
        value = row["params"].get("value")
        writer.writerow(
            [
                local.isoformat(timespec="seconds"),
                row["lever"],
                row["op"],
                "" if value is None else value,
                shown,
                row["why"] or "",
            ]
        )
    return Response(
        out.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="thermaestro-shadow.csv"'},
    )


# --- from NibePi ---------------------------------------------------------------------------


class NibePiFile(BaseModel):
    config: str
    """The file's text."""


class NibePiChoice(BaseModel):
    items: list[str]
    """The parts to make, as `<kind>:<id>` from the draft."""
    timezone: str | None = None
    model: str | None = None


@router.post("/import/nibepi")
@action("nibepi_import.read")
async def read_nibepi(body: NibePiFile, request: Request, who: Logged) -> dict[str, Any]:
    """Read NibePi's config.json into a draft, kept half an hour for this user: the parts
    it would make (`items`, each `{key, kind, id, body, what, optional}`), the secrets it
    would keep (by name only), every key's fate (`rows`), and notes. Nothing is made."""
    token, kept = await services(request).read_nibepi(who, body.config)
    draft = kept.draft
    return {
        "token": token,
        "line": draft.line,
        "items": [
            {
                "key": f"{i.kind}:{i.id}",
                "kind": i.kind,
                "id": i.id,
                "body": i.body,
                "what": i.what,
                "optional": i.optional,
            }
            for i in draft.items
        ],
        "secrets": sorted(draft.secrets),
        "rows": [{"key": r.key, "outcome": r.outcome, "note": r.note} for r in draft.rows],
        "notes": draft.notes,
        "timezone": draft.timezone,
        "localhost_broker": draft.localhost_broker,
        "broker_answers": kept.broker,
    }


@router.post("/import/nibepi/{token}")
@action("nibepi_import.apply")
async def apply_nibepi(
    token: str, body: NibePiChoice, request: Request, who: Logged
) -> dict[str, list[str]]:
    """Make the chosen parts of a draft, as setup would: `{done, problems}`."""
    return await services(request).apply_nibepi(
        who, token, body.items, timezone=body.timezone, model=body.model
    )
