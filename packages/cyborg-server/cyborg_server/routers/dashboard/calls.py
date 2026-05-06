"""Dashboard phone call routes."""

from __future__ import annotations

import logging
import traceback
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import FileResponse, HTMLResponse

from cyborg_server.database import Database
from cyborg_server.dependencies import get_database

from ._helpers import _get_pending_approval_count, _render_template

logger = logging.getLogger(__name__)

router = APIRouter()


def _parse_iso(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, TypeError):
        return None


def _offset_ms(call_start: datetime, exchange_start: datetime | None) -> int | None:
    if not exchange_start:
        return None
    return int((exchange_start - call_start).total_seconds() * 1000)


@router.get("/calls", response_class=HTMLResponse)
async def call_list(
    request: Request,
    db: Database = Depends(get_database),
) -> Response:
    pending_count = await _get_pending_approval_count(db)

    calls = await db.fetch_all(
        """SELECT id, call_sid, phone_number, direction, status, agenda,
                  exchange_count, duration_seconds, recording_path,
                  started_at, completed_at
           FROM phone_calls
           ORDER BY started_at DESC""",
    )

    stats = await db.fetch_one(
        """SELECT COUNT(*) as total,
                  AVG(duration_seconds) as avg_duration,
                  SUM(CASE WHEN recording_path IS NOT NULL THEN 1 ELSE 0 END) as recorded
           FROM phone_calls
           WHERE status = 'completed'""",
    )

    return _render_template("dashboard/calls.html", request, {
        "pending_count": pending_count,
        "calls": [dict(c) for c in calls],
        "total_calls": int(stats["total"] or 0) if stats else 0,
        "avg_duration": round(float(stats["avg_duration"] or 0), 1) if stats else 0,
        "recorded_count": int(stats["recorded"] or 0) if stats else 0,
    })


@router.get("/calls/{call_id}", response_class=HTMLResponse)
async def call_detail(
    call_id: str,
    request: Request,
    db: Database = Depends(get_database),
) -> Response:
    try:
        pending_count = await _get_pending_approval_count(db)

        call = await db.fetch_one(
            "SELECT * FROM phone_calls WHERE id = ?",
            (call_id,),
        )
        if not call:
            raise HTTPException(status_code=404, detail="Call not found")

        exchanges = await db.fetch_all(
            """SELECT exchange_index, user_transcript, assistant_transcript,
                      stt_ms, openclaw_ms, tts_first_chunk_ms, e2e_ms,
                      gateway_prepare_ms, gateway_stream_ms,
                      tts_wait_lock_ms, tts_generate_ms,
                      started_at, created_at
               FROM phone_call_exchanges
               WHERE call_id = ?
               ORDER BY exchange_index""",
            (call_id,),
        )

        call_data = dict(call)
        call_start = _parse_iso(call_data.get("started_at"))

        # Format call start time for display
        if call_start:
            call_data["started_at_display"] = call_start.strftime("%Y-%m-%d %H:%M:%S UTC")
        else:
            call_data["started_at_display"] = call_data.get("started_at") or "—"

        exchange_rows = []
        for e in exchanges:
            row = dict(e)
            ex_start = _parse_iso(row.get("started_at"))
            offset = _offset_ms(call_start, ex_start) if call_start and ex_start else None
            row["offset_ms"] = offset
            row["offset_display"] = f"{offset / 1000:.1f}s" if offset is not None else "—"
            row["time_display"] = ex_start.strftime("%H:%M:%S") if ex_start else "—"
            exchange_rows.append(row)

        return _render_template("dashboard/call_detail.html", request, {
            "pending_count": pending_count,
            "call": call_data,
            "exchanges": exchange_rows,
        })
    except HTTPException:
        raise
    except Exception:
        logger.error("call_detail error", exc_info=True)
        raise HTTPException(status_code=500, detail=traceback.format_exc())


@router.get("/calls/{call_id}/timeline", response_class=HTMLResponse)
async def call_timeline_partial(
    call_id: str,
    request: Request,
    db: Database = Depends(get_database),
) -> Response:
    """HTMX partial: returns just the timeline HTML for live refresh."""
    call = await db.fetch_one(
        "SELECT id, status, started_at FROM phone_calls WHERE id = ?",
        (call_id,),
    )
    if not call:
        raise HTTPException(status_code=404)

    exchanges = await db.fetch_all(
        """SELECT exchange_index, user_transcript, assistant_transcript,
                  stt_ms, openclaw_ms, tts_first_chunk_ms, e2e_ms,
                  gateway_prepare_ms, gateway_stream_ms,
                  tts_wait_lock_ms, tts_generate_ms,
                  started_at, created_at
           FROM phone_call_exchanges
           WHERE call_id = ?
           ORDER BY exchange_index""",
        (call_id,),
    )

    call_start = _parse_iso(call["started_at"])
    call_data = dict(call)
    call_data["id"] = call_id

    exchange_rows = []
    for e in exchanges:
        row = dict(e)
        ex_start = _parse_iso(row.get("started_at"))
        offset = _offset_ms(call_start, ex_start) if call_start and ex_start else None
        row["offset_ms"] = offset
        row["offset_display"] = f"{offset / 1000:.1f}s" if offset is not None else "—"
        row["time_display"] = ex_start.strftime("%H:%M:%S") if ex_start else "—"
        exchange_rows.append(row)

    # If call completed, stop polling by including the hx-disable marker
    return _render_template("dashboard/call_detail_timeline.html", request, {
        "call": call_data,
        "exchanges": exchange_rows,
    })


@router.get("/calls/{call_id}/audio")
async def serve_call_audio(
    call_id: str,
    request: Request,
    db: Database = Depends(get_database),
) -> Response:
    call = await db.fetch_one(
        "SELECT recording_path FROM phone_calls WHERE id = ?",
        (call_id,),
    )
    if not call or not call["recording_path"]:
        raise HTTPException(status_code=404, detail="Recording not found")

    settings = request.app.state.settings
    audio_path = settings.data_dir / "calls" / call["recording_path"]
    if not audio_path.is_file():
        raise HTTPException(status_code=404, detail="Recording file not found")

    return FileResponse(str(audio_path), media_type="audio/wav")
