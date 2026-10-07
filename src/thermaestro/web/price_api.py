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
                    {"layer": p.layer, "role": p.role, "value": p.value, "vat_added": p.vat_added}
                    for p in slot.parts
                ],
            }
            for slot in stack.slots
        ],
    }


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
