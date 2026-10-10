"""The prices page: what one more kWh costs today and tomorrow, and where the prices come
from. The sources, the layers of the stack and VAT are set under Setup → Prices; their
forms post here, each to an endpoint that does what its API counterpart does."""

from datetime import datetime, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, Response
from starlette.datastructures import FormData

from ..auth import AccountError
from . import i18n, labels
from .app import action, caller, services
from .control_pages import _rows, _text
from .operations import Caller
from .pages import Text, render
from .pages import _message as message
from .setup_pages import attempt, show

router = APIRouter(default_response_class=HTMLResponse)

Logged = Annotated[Caller, Depends(caller)]


@router.get("/prices")
async def prices_page(request: Request, who: Logged) -> Response:
    s = services(request)
    today = datetime.now(s.zone).date()
    days = []
    for day in (today, today + timedelta(days=1)):
        days.append((day, await s.price_stack(who, day)))
    return render(
        request,
        "prices.html",
        who,
        offered=s.offered_series(who),
        days=days,
        chart=await chart_config(request, who),
        decimal=i18n.decimal_symbol(),
        zone=i18n.zone_name(),
        formats=i18n.formats.get(),
    )


async def chart_config(request: Request, who: Caller) -> dict[str, Any]:
    """What the price chart needs besides the prices: the days, and each layer's name."""
    s = services(request)
    today = datetime.now(s.zone).date()
    layers = await s.price_layers(who)
    names = {
        id: labels.role(layer.role) + (f" \N{MIDDLE DOT} {layer.plugin}" if layer.plugin else "")
        for id, layer in layers.items()
    }
    return {
        "names": {**names, "vat": labels.role("vat")},
        "days": [d.isoformat() for d in (today, today + timedelta(days=1))],
        "day_names": [i18n._("Today"), i18n._("Tomorrow")],
        "total": i18n._("Total"),
        "empty": i18n._("No prices for this day yet."),
        "fallback": i18n._("* from a fallback series, where the layer's own has no price."),
    }


@router.post("/prices/layers")
@action("price_layer.write")
async def add_layer(
    request: Request,
    who: Logged,
    source: Text,
    offered: Text = "",
    role: Text = "",
    value: Text = "",
    unit: Text = "",
    vat: Text = "excl",
) -> Response:
    s = services(request)
    body: dict[str, Any]
    if source == "series":
        found = next(
            (o for o in s.offered_series(who) if f"{o['instance']}:{o['series']}" == offered), None
        )
        if found is None:
            return await show(
                request, who, "prices", 400, error=message(AccountError("no such series"))
            )
        body = {
            "role": found["role"],
            "source": "series",
            "plugin": found["instance"],
            "series": found["series"],
            "unit": found["unit"],
            "vat": found["vat"] if found["vat"] in ("incl", "excl") else "excl",
        }
    else:
        try:
            amount = float(value.replace(",", "."))
        except ValueError:
            return await show(
                request, who, "prices", 400, error=message(AccountError("not a number"))
            )
        body = {"role": role, "source": "fixed", "value": amount, "unit": unit.strip(), "vat": vat}
    return await attempt(request, who, s.set_price_layer(who, None, body), "prices")


@router.post("/prices/layers/{id}/delete")
@action("price_layer.delete")
async def delete_layer(request: Request, who: Logged, id: str) -> Response:
    return await attempt(request, who, services(request).delete_price_layer(who, id), "prices")


@router.post("/prices/layers/{id}/fallbacks")
@action("price_layer.write")
async def set_fallbacks(
    request: Request,
    who: Logged,
    id: str,
    fallbacks: Annotated[list[str] | None, Form()] = None,
) -> Response:
    return await attempt(
        request, who, services(request).set_fallbacks(who, id, list(fallbacks or [])), "prices"
    )


@router.post("/settings/energidataservice")
@action("danish_grid.write")
async def choose_danish_grid(request: Request, who: Logged, choice: Text = "") -> Response:
    """A grid company and tariff from the list, as `<gln>:<tariff>`."""
    gln, _, tariff = choice.partition(":")
    work = services(request).choose_danish_grid(who, gln, tariff)
    return await attempt(request, who, work, "prices", "/setup/prices#denmark")


# --- grid rules --------------------------------------------------------------------------


def _when(form: FormData, prefix: str) -> dict[str, Any]:
    found = form.getlist(f"{prefix}.months")
    months = sorted({int(m) for m in found if isinstance(m, str) and m.isdigit()})
    return {
        "months": months,
        "days": _text(form, f"{prefix}.days") or "all",
        "start": _text(form, f"{prefix}.start") or None,
        "end": _text(form, f"{prefix}.end") or None,
    }


def _amount(form: FormData, name: str) -> float | None:
    text = _text(form, name).replace(",", ".")
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        raise AccountError(f"{name} is a number") from None


def rule_from_form(form: FormData) -> dict[str, Any]:
    """A grid rule from Setup → Prices: what a field left empty says is that the grid
    company hasn't given it."""
    kind = _text(form, "type")
    body: dict[str, Any] = {
        "type": kind,
        "owner": _text(form, "owner"),
        "status": _text(form, "status") or "in_force",
        "clock": _text(form, "clock") or "civil",
        "vat": _text(form, "vat") or "excl",
    }
    for name in ("valid_from", "valid_to", "unit", "note"):
        if _text(form, name):
            body[name] = _text(form, name)
    unknown: list[str] = []
    if kind == "tou":
        body["base"] = _amount(form, "base")
        body["rates"] = [
            {**_when(form, f"r{i}"), "price": price}
            for i in _rows(form, "r")
            if (price := _amount(form, f"r{i}.price")) is not None
        ]
    elif kind == "interval_peak":
        body["window"] = [
            w
            for i in _rows(form, "w")
            if (w := _when(form, f"w{i}"))["months"] or w["days"] != "all" or w["start"] or w["end"]
        ]
        for name in ("interval_minutes", "peaks"):
            found = _amount(form, name)
            body[name] = None if found is None else int(found)
        body["price_per_kw"] = _amount(form, "price_per_kw")
        answer = _text(form, "different_days")
        body["different_days"] = {"yes": True, "no": False}.get(answer)
    elif kind == "subscribed_power":
        body["kw"] = _amount(form, "kw")
    for name in ("base", "interval_minutes", "peaks", "different_days", "price_per_kw"):
        if name in body and body[name] is None:
            unknown.append(name)
            del body[name]
    body["unknown"] = unknown
    return body


@router.post("/prices/grid-rules")
@action("grid_rule.write")
async def add_grid_rule(request: Request, who: Logged) -> Response:
    s = services(request)
    try:
        body = rule_from_form(await request.form())
    except AccountError as e:
        return await show(request, who, "prices", 400, error=message(e))
    return await attempt(
        request, who, s.set_grid_rule(who, None, body), "prices", "/setup/prices#grid"
    )


@router.post("/prices/grid-rules/{id}")
@action("grid_rule.write")
async def put_grid_rule(request: Request, who: Logged, id: str) -> Response:
    s = services(request)
    try:
        body = rule_from_form(await request.form())
    except AccountError as e:
        return await show(request, who, "prices", 400, error=message(e))
    return await attempt(
        request, who, s.set_grid_rule(who, id, body), "prices", "/setup/prices#grid"
    )


@router.post("/prices/grid-rules/{id}/delete")
@action("grid_rule.delete")
async def delete_grid_rule(request: Request, who: Logged, id: str) -> Response:
    work = services(request).delete_grid_rule(who, id)
    return await attempt(request, who, work, "prices", "/setup/prices#grid")


@router.post("/settings/tibber")
@action("price_source.write")
async def set_tibber(
    request: Request, who: Logged, id: Text = "tibber", token: Text = ""
) -> Response:
    s = services(request)
    current = (await s.price_sources(who)).get(id)
    token_name = f"{id.lower()}.token"

    async def work() -> None:
        if not token and current is None:
            raise AccountError("enter the Tibber token")
        if token:
            await s.set_secret(who, token_name, token.strip())
        home = current["settings"].get("home") if current else None
        await s.set_price_source(who, "tibber", id, {"token": token_name, "home": home})

    return await attempt(request, who, work(), "prices", "/setup/prices#sources")


@router.post("/settings/entsoe")
@action("price_source.write")
async def set_entsoe(
    request: Request,
    who: Logged,
    zone: Text,
    id: Text = "entsoe",
    token: Text = "",
    currency: Text = "",
) -> Response:
    from ..zones import ZONES

    s = services(request)
    current = (await s.price_sources(who)).get(id)
    token_name = f"{id.lower()}.token"

    async def work() -> None:
        if zone not in ZONES:
            raise AccountError(f"no bidding zone {zone!r}")
        if not token and current is None:
            raise AccountError("enter the ENTSO-E token")
        if token:
            await s.set_secret(who, token_name, token.strip())
        settings = {
            "token": token_name,
            "zone": zone,
            "currency": currency.strip().upper() or ZONES[zone].currency,
        }
        await s.set_price_source(who, "entsoe", id, settings)

    return await attempt(request, who, work(), "prices", "/setup/prices#sources")


@router.post("/settings/spot")
@action("price_source.write")
async def set_spot(
    request: Request,
    who: Logged,
    zone: Text,
    source: Text,
    fallback: Text = "",
    currency: Text = "",
) -> Response:
    work = services(request).choose_spot(who, zone, source, fallback or None, currency or None)
    return await attempt(request, who, work, "prices", "/setup/prices#spot")


@router.post("/settings/octopus_agile")
@action("price_source.write")
async def set_octopus_agile(
    request: Request, who: Logged, region: Text, id: Text = "octopus_agile"
) -> Response:
    work = services(request).set_price_source(
        who, "octopus_agile", id, {"region": region.strip().upper()}
    )
    return await attempt(request, who, work, "prices", "/setup/prices#sources")


@router.post("/prices/vat")
@action("vat.write")
async def set_vat(
    request: Request,
    who: Logged,
    rate: Text,
    applies_to: Annotated[list[str] | None, Form()] = None,
) -> Response:
    try:
        fraction = float(rate.replace(",", ".")) / 100
    except ValueError:
        return await show(request, who, "prices", 400, error=message(AccountError("not a number")))
    body = {"rate": fraction, "applies_to": applies_to or []}
    return await attempt(
        request, who, services(request).set_vat(who, body), "prices", "/setup/prices#vat"
    )
