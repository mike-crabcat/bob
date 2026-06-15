"""Dashboard API: Phone calls and recordings."""

from __future__ import annotations

from fastapi import APIRouter

from bob_server.routers.dashboard_api._common import *  # noqa: F403,F405


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
    rows = await db.fetch_all(
        """SELECT pc.id, pc.call_sid, pc.phone_number, pc.direction, pc.status,
                  pc.agenda, pc.exchange_count, pc.duration_seconds, pc.recording_path,
                  pc.started_at, pc.completed_at,
                  c.id as contact_id, c.name as contact_name
           FROM phone_calls pc
           LEFT JOIN contacts c ON c.phone_number = pc.phone_number AND c.deleted_at IS NULL
           ORDER BY pc.started_at DESC
           LIMIT 50"""
    )
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
    call = await db.fetch_one(
        """SELECT pc.id, pc.call_sid, pc.phone_number, pc.direction, pc.status,
                  pc.agenda, pc.exchange_count, pc.duration_seconds, pc.recording_path,
                  pc.started_at, pc.completed_at,
                  c.id as contact_id, c.name as contact_name
           FROM phone_calls pc
           LEFT JOIN contacts c ON c.phone_number = pc.phone_number AND c.deleted_at IS NULL
           WHERE pc.id = ? OR pc.call_sid = ?""",
        (call_id, call_id),
    )
    if not call:
        return {"error": "Call not found"}
    exchanges = await db.fetch_all(
        """SELECT exchange_index, user_transcript, assistant_transcript,
                  stt_ms, llm_total_ms, tts_first_chunk_ms, e2e_ms,
                  started_at, created_at
           FROM phone_call_exchanges
           WHERE call_id = ?
           ORDER BY exchange_index""",
        (call["id"],),
    )
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

    from bob_server.routers.phone import initiate_outbound_call
    return await initiate_outbound_call(
        db=_db(request),
        settings=request.app.state.settings,
        phone_settings=phone_settings,
        to_number=to_number,
        agenda=agenda,
        app_state=request.app.state,
    )


@router.get("/api/phone/recording/{call_id}")
async def get_phone_recording(request: Request, call_id: str) -> Any:
    if not _check_auth(request):
        return {"error": "unauthorized"}
    db = _db(request)
    call = await db.fetch_one(
        "SELECT recording_path FROM phone_calls WHERE id = ? OR call_sid = ?",
        (call_id, call_id),
    )
    if not call or not call["recording_path"]:
        return {"error": "No recording available"}
    rec_path = Path(call["recording_path"])
    if not rec_path.is_file():
        return {"error": "Recording file not found"}
    return FileResponse(rec_path, media_type="audio/wav")


# ── Persona ──────────────────────────────────────────────────────────

