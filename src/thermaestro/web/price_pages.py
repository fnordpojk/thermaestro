"""The prices page: the series offered, the layers of the stack, VAT, and what one more
kWh costs today and tomorrow. Each form posts to an endpoint that does what its API
counterpart does."""

from datetime import datetime, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, Response

from ..auth import AccountError
from .app import action, caller, services
from .operations import Caller
from .pages import Text, back, render
from .pages import _message as message

router = APIRouter(default_response_class=HTMLResponse)

Logged = Annotated[Caller, Depends(caller)]

FIXED_ROLES = (
    "energy.supplier",
    "tax.energy",
    "grid.transfer",
    "grid.tou",
    "levy",
    "subsidy",
    "energy.spot",
)


async def _prices(request: Request, who: Caller, status_code: int = 200, **extra: Any) -> Response:
    s = services(request)
    today = datetime.now(s.zone).date()
    days = []
    for day in (today, today + timedelta(days=1)):
        days.append((day, await s.price_stack(who, day)))
    return render(
        request,
        "prices.html",
        who,
        status_code=status_code,
        offered=s.offered_series(who),
        layers=await s.price_layers(who),
        vat=await s.vat(who),
        days=days,
        fixed_roles=FIXED_ROLES,
        **extra,
    )


@router.get("/prices")
async def prices_page(request: Request, who: Logged) -> Response:
    return await _prices(request, who)


async def _attempt(request: Request, who: Caller, work: Any) -> Response:
    try:
        await work
    except AccountError as e:
        return await _prices(request, who, 400, error=message(e))
    return back("/prices")


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
            return await _prices(request, who, 400, error=message(AccountError("no such series")))
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
            return await _prices(request, who, 400, error=message(AccountError("not a number")))
        body = {"role": role, "source": "fixed", "value": amount, "unit": unit.strip(), "vat": vat}
    return await _attempt(request, who, s.set_price_layer(who, None, body))


@router.post("/prices/layers/{id}/delete")
@action("price_layer.delete")
async def delete_layer(request: Request, who: Logged, id: str) -> Response:
    return await _attempt(request, who, services(request).delete_price_layer(who, id))


@router.post("/prices/layers/{id}/fallbacks")
@action("price_layer.write")
async def set_fallbacks(
    request: Request,
    who: Logged,
    id: str,
    fallbacks: Annotated[list[str] | None, Form()] = None,
) -> Response:
    return await _attempt(
        request, who, services(request).set_fallbacks(who, id, list(fallbacks or []))
    )


# --- the sources, on the settings page ----------------------------------------------------


@router.post("/settings/tibber")
@action("price_source.write")
async def set_tibber(
    request: Request, who: Logged, id: Text = "tibber", token: Text = ""
) -> Response:
    from .sensor_pages import _settings_attempt

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

    return await _settings_attempt(request, who, work(), "/settings#prices")


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
    from ..entsoe.zones import ZONES
    from .sensor_pages import _settings_attempt

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

    return await _settings_attempt(request, who, work(), "/settings#prices")


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
        return await _prices(request, who, 400, error=message(AccountError("not a number")))
    body = {"rate": fraction, "applies_to": applies_to or []}
    return await _attempt(request, who, services(request).set_vat(who, body))
