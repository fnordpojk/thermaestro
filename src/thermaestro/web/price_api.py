"""The API for prices: the series the plugins offer, the layers of the stack, VAT, and
the stack per day."""

from datetime import date, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request

from ..auth import AccountError
from .app import action, caller, services
from .operations import Caller

router = APIRouter(prefix="/api/v1")

Logged = Annotated[Caller, Depends(caller)]


@router.get("/series")
@action("series.read")
async def offered_series(request: Request, who: Logged) -> list[dict[str, Any]]:
    """Every series the plugins offer, and whether it holds what it should by now."""
    return services(request).offered_series(who)


@router.get("/prices")
@action("prices.read")
async def prices(
    request: Request, who: Logged, day: Annotated[str | None, Query()] = None
) -> dict[str, Any]:
    """What one more kWh costs per 15-minute slot of a day (default: today), with each
    layer's part; or why the stack is refused."""
    s = services(request)
    try:
        chosen = date.fromisoformat(day) if day else datetime.now(s.zone).date()
    except ValueError:
        raise AccountError("the day is written YYYY-MM-DD") from None
    stack = await s.price_stack(who, chosen)
    return {
        "day": chosen.isoformat(),
        "unit": stack.unit,
        "problems": stack.problems,
        "warnings": stack.warnings,
        "slots": [
            {
                "start": slot.start.isoformat(),
                "end": slot.end.isoformat(),
                "total": slot.total,
                "missing": slot.missing,
                "parts": [
                    {
                        "layer": p.layer,
                        "role": p.role,
                        "value": p.value,
                        "vat_added": p.vat_added,
                        "fallback": p.fallback,
                        "carried_from": p.carried_from.isoformat() if p.carried_from else None,
                    }
                    for p in slot.parts
                ],
            }
            for slot in stack.slots
        ],
        "checks": [
            {
                "series": c.series,
                "layers": c.layers,
                "compared": c.compared,
                "differing": c.differing,
                "largest": c.largest,
                "at": c.at.isoformat() if c.at else None,
            }
            for c in stack.checks
        ],
    }


@router.get("/prices/sources")
@action("price_sources.read")
async def sources(request: Request, who: Logged) -> dict[str, Any]:
    """The price plugins' instances, with their settings and how they are doing."""
    return await services(request).price_sources(who)


@router.put("/prices/sources/{id}")
@action("price_source.write")
async def set_source(
    id: str, body: dict[str, Any], request: Request, who: Logged
) -> dict[str, Any]:
    """Add or change a price source: `{"plugin": "tibber", "settings": {"token": ...}}`;
    the token is a secret's name, entered through the secrets API."""
    plugin = body.get("plugin")
    settings = body.get("settings")
    if not isinstance(plugin, str) or not isinstance(settings, dict):
        raise AccountError("give the plugin and its settings")
    setting = await services(request).set_price_source(who, plugin, id, settings)
    return setting.model_dump(mode="json")


@router.get("/prices/spot")
@action("price_sources.read")
async def spot(request: Request, who: Logged) -> dict[str, Any]:
    """Where the spot price comes from, as plugin names, and the bidding zone."""
    return await services(request).spot_choice(who)


@router.put("/prices/spot")
@action("price_source.write")
async def set_spot(body: dict[str, Any], request: Request, who: Logged) -> dict[str, Any]:
    """Take a bidding zone's spot price from a source, with another standing in:
    `{"zone": "NO1", "source": "energy_charts", "fallback": "nordic_sites"}`, and
    optionally `"currency"`. The sources a zone has are those Setup → Prices offers."""
    zone, source = body.get("zone"), body.get("source")
    fallback, currency = body.get("fallback"), body.get("currency")
    if not isinstance(zone, str) or not isinstance(source, str):
        raise AccountError("give the zone and the source")
    if not isinstance(fallback, str | None) or not isinstance(currency, str | None):
        raise AccountError("the fallback and the currency are names")
    id, layer = await services(request).choose_spot(who, zone, source, fallback, currency)
    return {"id": id, **layer.model_dump(mode="json")}


@router.get("/prices/layers")
@action("price_layers.read")
async def layers(request: Request, who: Logged) -> dict[str, Any]:
    found = await services(request).price_layers(who)
    return {id: layer.model_dump(mode="json") for id, layer in found.items()}


@router.post("/prices/layers", status_code=201)
@action("price_layer.write")
async def add_layer(body: dict[str, Any], request: Request, who: Logged) -> dict[str, Any]:
    id, layer = await services(request).set_price_layer(who, None, body)
    return {"id": id, **layer.model_dump(mode="json")}


@router.put("/prices/layers/{id}")
@action("price_layer.write")
async def set_layer(id: str, body: dict[str, Any], request: Request, who: Logged) -> dict[str, Any]:
    _, layer = await services(request).set_price_layer(who, id, body)
    return {"id": id, **layer.model_dump(mode="json")}


@router.delete("/prices/layers/{id}")
@action("price_layer.delete")
async def delete_layer(id: str, request: Request, who: Logged) -> dict[str, str]:
    await services(request).delete_price_layer(who, id)
    return {"status": "ok"}


@router.get("/prices/vat")
@action("vat.read")
async def vat(request: Request, who: Logged) -> dict[str, Any] | None:
    found = await services(request).vat(who)
    return found.model_dump(mode="json") if found else None


@router.put("/prices/vat")
@action("vat.write")
async def set_vat(body: dict[str, Any], request: Request, who: Logged) -> dict[str, Any]:
    """The rate (0.25 for 25 %) and the layers it is charged on, by id or role."""
    return (await services(request).set_vat(who, body)).model_dump(mode="json")


@router.get("/prices/grid-rules")
@action("grid_rules.read")
async def grid_rules(request: Request, who: Logged) -> dict[str, Any]:
    """The grid rules entered, by id."""
    found = await services(request).grid_rules(who)
    return {id: rule.model_dump(mode="json") for id, rule in found.items()}


@router.post("/prices/grid-rules", status_code=201)
@action("grid_rule.write")
async def add_grid_rule(body: dict[str, Any], request: Request, who: Logged) -> dict[str, Any]:
    """A grid company's rule: `{"type": "tou", "owner": "…", "unit": "SEK/kWh", "base":
    0.18, "rates": [{"months": [11, 12, 1, 2, 3], "days": "working_days", "start": "07:00",
    "end": "20:00", "price": 0.52}]}`, an `interval_peak` or a `subscribed_power`. A field
    the grid company hasn't given is listed in `unknown`. A time-of-use rule becomes a
    layer of the price stack."""
    id, rule = await services(request).set_grid_rule(who, None, body)
    return {"id": id, **rule.model_dump(mode="json")}


@router.put("/prices/grid-rules/{id}")
@action("grid_rule.write")
async def put_grid_rule(
    id: str, body: dict[str, Any], request: Request, who: Logged
) -> dict[str, Any]:
    _, rule = await services(request).set_grid_rule(who, id, body)
    return {"id": id, **rule.model_dump(mode="json")}


@router.delete("/prices/grid-rules/{id}")
@action("grid_rule.delete")
async def delete_grid_rule(id: str, request: Request, who: Logged) -> dict[str, str]:
    """Remove a rule, and the layer that holds its prices."""
    await services(request).delete_grid_rule(who, id)
    return {"status": "ok"}
