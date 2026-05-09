"""API endpoints for WhatsApp bridge control."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from cyborg_server.dependencies import get_app_context
from cyborg_server.context import AppContext


router = APIRouter(prefix="/whatsapp", tags=["whatsapp"])


class PairRequest(BaseModel):
    method: str = "qr"
    phone_number: str | None = None


class SendRequest(BaseModel):
    chat_id: str
    text: str
    reply_to_message_id: str | None = None


def _get_bridge_service(request: Request):
    svc = getattr(request.app.state, "whatsapp_bridge_service", None)
    if svc is None:
        raise HTTPException(status_code=503, detail="WhatsApp bridge service not enabled")
    return svc


@router.get("/status")
async def whatsapp_status(request: Request) -> dict[str, Any]:
    svc = _get_bridge_service(request)
    return {
        "bridge_connected": svc.connected,
        "enabled": True,
    }


@router.post("/pair")
async def whatsapp_pair(
    body: PairRequest,
    request: Request,
) -> dict[str, Any]:
    svc = _get_bridge_service(request)
    if not svc.connected:
        raise HTTPException(status_code=503, detail="Not connected to bridge")
    result = await svc.request_pairing(method=body.method, phone_number=body.phone_number)
    return result


@router.post("/send")
async def whatsapp_send(
    body: SendRequest,
    request: Request,
) -> dict[str, Any]:
    svc = _get_bridge_service(request)
    if not svc.connected:
        raise HTTPException(status_code=503, detail="Not connected to bridge")
    request_id = await svc.send_message(
        body.chat_id, body.text, reply_to=body.reply_to_message_id,
    )
    return {"request_id": request_id, "status": "sent"}


@router.get("/bridge-status")
async def whatsapp_bridge_status(request: Request) -> dict[str, Any]:
    svc = _get_bridge_service(request)
    return await svc.get_bridge_status()
