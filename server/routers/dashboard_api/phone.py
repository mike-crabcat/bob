"""Dashboard API: Phone calls and recordings."""

from __future__ import annotations

from fastapi import APIRouter

from server.routers.dashboard_api._common import *  # noqa: F403,F405


router = APIRouter()


@router.get("/api/phone/calls")
async def get_phone_calls(request: Request) -> dict[str, Any]:
    if not _check_auth(request):
        return {"error": "unauthorized"}
    db = _db(request)
    table_exists = await db.fetch_one(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='phone_calls'"
    )
    if not table_exists:
        return {"calls": []}
    from server.repositories.phone_calls import PhoneCallRepository
    rows = await PhoneCallRepository(db).recent_with_contacts(limit=50)
    calls: list[dict[str, Any]] = []
    for row in rows:
        calls.append({
            "id": row["id"],
            "call_sid": row["call_sid"],
            "phone_number": row["phone_number"],
            "direction": row["direction"],
            "status": row["status"],
            "agenda": row["agenda"],
            "exchange_count": row["exchange_count"] or 0,
            "duration_seconds": row["duration_seconds"],
            "recording_path": row["recording_path"],
            "started_at": _utc(row["started_at"]),
            "completed_at": _utc(row["completed_at"]),
            "contact_id": row["contact_id"],
            "contact_name": row["contact_name"],
        })
    return {"calls": calls}


@router.get("/api/phone/calls/{call_id}")
async def get_phone_call_detail(request: Request, call_id: str) -> dict[str, Any]:
    if not _check_auth(request):
        return {"error": "unauthorized"}
    db = _db(request)
    from server.repositories.phone_calls import PhoneCallRepository
    call = await PhoneCallRepository(db).detail_with_contact(call_id)
    if not call:
        return {"error": "Call not found"}
    exchanges: list = []
    return {
        "call": {
            "id": call["id"],
            "call_sid": call["call_sid"],
            "phone_number": call["phone_number"],
            "direction": call["direction"],
            "status": call["status"],
            "agenda": call["agenda"],
            "exchange_count": call["exchange_count"] or 0,
            "duration_seconds": call["duration_seconds"],
            "recording_path": call["recording_path"],
            "started_at": _utc(call["started_at"]),
            "completed_at": _utc(call["completed_at"]),
            "transcript": call["transcript"],
            "outcome": json.loads(call["outcome"]) if call["outcome"] else None,
            "contact_id": call["contact_id"],
            "contact_name": call["contact_name"],
        },
        "exchanges": [dict(e) for e in exchanges],
    }


@router.post("/api/phone/call")
async def dashboard_initiate_call(request: Request) -> dict[str, Any]:
    if not _check_auth(request):
        return {"error": "unauthorized"}
    body = await request.json()
    to_number = body.get("to", "").strip()
    if not to_number:
        return {"error": "Missing 'to' phone number"}
    agenda = body.get("agenda", "").strip()
    phone_settings = request.app.state.settings.phone
    if not phone_settings.enabled:
        return {"error": "Phone subsystem is not enabled"}

    from server.services.voice_dispatch_service import (
        build_outbound_instructions,
        initiate_outbound_call,
    )
    instructions = build_outbound_instructions(goal=agenda)
    return await initiate_outbound_call(
        db=_db(request),
        settings=request.app.state.settings,
        phone_settings=phone_settings,
        to_number=to_number,
        agenda=agenda,
        event_bus=request.app.state.event_bus,
        engine="openai_realtime",
        realtime_meta={"instructions": instructions, "voice": ""},
    )


@router.post("/api/phone/calls/{call_id}/hangup")
async def hangup_phone_call(request: Request, call_id: str) -> dict[str, Any]:
    """Hang up an active or ringing call. Mirrors the webhook-router endpoint,
    dashboard-authenticated, via the shared voice_dispatch_service owner."""
    if not _check_auth(request):
        return {"error": "unauthorized"}
    db = _db(request)
    from server.repositories.phone_calls import PhoneCallRepository
    call = await PhoneCallRepository(db).get(call_id)
    if not call:
        return {"error": "Call not found"}
    if call["status"] not in ("active", "ringing"):
        return {"error": f"Call is {call['status']}, cannot hang up"}

    from server.services.voice_dispatch_service import hangup_twilio_call

    if hangup_twilio_call(request.app.state.settings, call["call_sid"]):
        return {"ok": True}
    return {"error": "Failed to hang up call via Twilio"}


@router.get("/api/phone/recording/{call_id}")
async def get_phone_recording(request: Request, call_id: str) -> Any:
    if not _check_auth(request):
        return {"error": "unauthorized"}
    db = _db(request)
    from server.repositories.phone_calls import PhoneCallRepository
    call = await PhoneCallRepository(db).get(call_id)
    if not call or not call["recording_path"]:
        return {"error": "No recording available"}
    rec_path = Path(call["recording_path"])
    if not rec_path.is_file():
        return {"error": "Recording file not found"}
    return FileResponse(rec_path, media_type="audio/wav")


# ── Persona ──────────────────────────────────────────────────────────

