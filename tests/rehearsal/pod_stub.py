"""Local print-on-demand stub for the rehearsal (bob-events-plan.md §4.3).

Implements the endpoints Bob's printful skill uses — catalogue lookup and
POST /v2/orders — asserting the properties the skill's discipline depends
on: bearer auth against the configured key, and ``external_id`` idempotency
(a replayed order returns the original instead of double-ordering).

In-process (pytest): ``stub_client(stub)`` returns an httpx client bound to
the ASGI app. Standalone (live rehearsal): ``python -m
tests.rehearsal.pod_stub`` serves it on a port; point the skill's
``--api-base`` at it.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Header, HTTPException, Request


def make_pod_stub(api_key: str = "rehearsal-key") -> FastAPI:
    app = FastAPI(title="Printful rehearsal stub")
    state: dict[str, Any] = {"next_id": 4100, "orders": [], "by_external": {}}

    @app.get("/v2/products")
    async def products(limit: int = 20) -> dict[str, Any]:
        return {"data": [{"id": 71, "name": "Bella Canvas 3001",
                          "brand": "Bella+Canvas", "model": "3001"},
                         {"id": 145, "name": "Gildan 18000",
                          "brand": "Gildan", "model": "18000"}]}

    @app.get("/v2/products/{product_id}")
    async def product(product_id: int) -> dict[str, Any]:
        return {"result": {"id": product_id, "name": "Bella Canvas 3001",
                           "variants": [{"id": 4012, "name": "S, Black"},
                                        {"id": 4013, "name": "M, Black"},
                                        {"id": 4014, "name": "L, Black"}]}}

    @app.get("/v2/orders/{order_id}")
    async def get_order(order_id: int,
                        authorization: str = Header(default="")) -> dict[str, Any]:
        if authorization != f"Bearer {api_key}":
            raise HTTPException(status_code=401, detail="bad key")
        for order in state["orders"]:
            if order["id"] == order_id:
                return {"result": order}
        raise HTTPException(status_code=404, detail="unknown order")

    @app.post("/v2/orders/{order_id}/confirm")
    async def confirm_order(order_id: int,
                            authorization: str = Header(default="")) -> dict[str, Any]:
        if authorization != f"Bearer {api_key}":
            raise HTTPException(status_code=401, detail="bad key")
        for order in state["orders"]:
            if order["id"] == order_id:
                order["status"] = "confirmed"
                return {"result": order}
        raise HTTPException(status_code=404, detail="unknown order")

    @app.post("/v2/orders")
    async def create_order(request: Request,
                           authorization: str = Header(default="")) -> dict[str, Any]:
        if authorization != f"Bearer {api_key}":
            raise HTTPException(status_code=401, detail="bad key")
        body = await request.json()
        external_id = body.get("external_id")
        if not external_id:
            raise HTTPException(status_code=400, detail="external_id required")
        if external_id in state["by_external"]:
            # Idempotent replay — the property crash-retry relies on.
            return {"result": state["by_external"][external_id]}
        if not body.get("items"):
            raise HTTPException(status_code=400, detail="empty cart")
        state["next_id"] += 1
        order = {"id": state["next_id"], "code": f"REH{state['next_id']}",
                 "external_id": external_id, "status": "draft"}
        state["orders"].append(order)
        state["by_external"][external_id] = order
        return {"result": order}

    app.state.pod_state = state
    return app


def stub_client(stub: FastAPI):
    """An httpx client bound to the stub in-process (ASGI transport)."""
    import httpx
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=stub))  # type: ignore[arg-type]


if __name__ == "__main__":  # standalone server for the live rehearsal
    import uvicorn
    uvicorn.run(make_pod_stub(), host="127.0.0.1", port=8477)
