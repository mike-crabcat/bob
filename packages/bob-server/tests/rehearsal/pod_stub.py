"""Local print-on-demand stub for the rehearsal (bob-events-plan.md §4.3).

Implements the endpoint the merch executor calls — POST /v2/orders — plus a
catalogue lookup, asserting the properties the gate depends on: bearer auth
against the configured key, and ``external_id`` idempotency (a replayed
order returns the original instead of double-ordering).

In-process (pytest): ``install_pod_stub(monkeypatch, stub)`` reroutes
merch_service's httpx onto the ASGI app. Standalone (live rehearsal):
``python -m tests.rehearsal.pod_stub`` serves it on a port; point
BOB_MERCH_API_BASE at it.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Header, HTTPException, Request


def make_pod_stub(api_key: str = "rehearsal-key") -> FastAPI:
    app = FastAPI(title="Printful rehearsal stub")
    state: dict[str, Any] = {"next_id": 4100, "orders": [], "by_external": {}}

    @app.get("/v2/products/{product_id}")
    async def product(product_id: int) -> dict[str, Any]:
        return {"result": {"id": product_id, "name": "Bella Canvas 3001",
                           "variants": [{"id": 4012, "name": "S, Black"},
                                        {"id": 4013, "name": "M, Black"},
                                        {"id": 4014, "name": "L, Black"}]}}

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


def install_pod_stub(monkeypatch, stub: FastAPI) -> dict[str, Any]:
    """Route merch_service's httpx onto the stub in-process."""
    import httpx
    import bob_server.services.merch_service as merch

    transport = httpx.ASGITransport(app=stub)  # type: ignore[arg-type]

    def _client(**kwargs):
        kwargs.pop("transport", None)
        return httpx.AsyncClient(transport=transport, **kwargs)

    monkeypatch.setattr(
        merch, "httpx",
        type("_HttpxShim", (), {"AsyncClient": staticmethod(_client)}))
    return stub.state.pod_state


if __name__ == "__main__":  # standalone server for the live rehearsal
    import uvicorn
    uvicorn.run(make_pod_stub(), host="127.0.0.1", port=8477)
