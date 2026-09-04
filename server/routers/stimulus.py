"""Stimulus ingest — the front door for external feeds (docs/stimulus-spine-plan.md).

POST /api/v1/stimulus/events with a dedicated bearer token. Deliberately
dumb: validate envelope → repository insert → return. The heartbeat router
drains the table; this endpoint never fans out.
"""

from __future__ import annotations

import hmac
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status

from server.context import AppContext
from server.dependencies import get_app_context
from server.repositories.stimulus import StimulusRepository

router = APIRouter(prefix="/stimulus", tags=["stimulus"])

_MAX_SUMMARY = 500
_MAX_BODY_CHARS = 20_000


def _bearer_token(request: Request) -> str:
    auth = request.headers.get("Authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return ""


def _valid_envelope(data: Any) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            "body must be a JSON object")
    source = data.get("source")
    type_ = data.get("type")
    if not isinstance(source, str) or not source.strip():
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            "'source' is required (non-empty string)")
    if not isinstance(type_, str) or not type_.strip():
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            "'type' is required (non-empty string)")
    level = data.get("level", "info")
    if level not in ("info", "action"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            "'level' must be 'info' or 'action'")
    ttl_s = data.get("ttl_s")
    if ttl_s is not None and (not isinstance(ttl_s, int) or not 1 <= ttl_s <= 86_400):
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            "'ttl_s' must be an int in [1, 86400]")
    dedup_key = data.get("dedup_key")
    if dedup_key is not None and not isinstance(dedup_key, str):
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            "'dedup_key' must be a string")
    summary = data.get("summary", "")
    if not isinstance(summary, str) or len(summary) > _MAX_SUMMARY:
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            f"'summary' must be a string ≤ {_MAX_SUMMARY} chars")
    body = data.get("body", {})
    if not isinstance(body, dict):
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            "'body' must be a JSON object")
    import json
    if len(json.dumps(body)) > _MAX_BODY_CHARS:
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            f"'body' too large (> {_MAX_BODY_CHARS} chars)")
    target_hint = data.get("target_hint")
    if target_hint is not None and not isinstance(target_hint, str):
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            "'target_hint' must be a string")
    return {
        "source": source.strip(), "type": type_.strip(), "level": level,
        "ttl_s": ttl_s, "dedup_key": dedup_key, "summary": summary,
        "body": body, "target_hint": target_hint,
    }


@router.post("/events")
async def post_stimulus_event(
    request: Request,
    ctx: AppContext = Depends(get_app_context),
) -> Any:
    token = ctx.settings.stimulus_token
    if not token:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE,
                            "stimulus ingest not configured (no token)")
    if not hmac.compare_digest(_bearer_token(request), token):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED,
                            "Unauthorized: valid stimulus token required",
                            headers={"WWW-Authenticate": 'Bearer realm="bob-stimulus"'})

    try:
        data = _valid_envelope(await request.json())
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "body must be JSON")

    # dedup_key is the idempotency contract; synthesise one so retries of
    # an envelope without a key still collapse within the same second.
    dedup_key = data["dedup_key"] or (
        f"{data['source']}:{data['type']}:{datetime.now(timezone.utc).isoformat(timespec='seconds')}")
    ts = datetime.now(timezone.utc).isoformat()

    repo = StimulusRepository(ctx.db)
    event_id, inserted = await repo.insert_event(
        source=data["source"], type_=data["type"], level=data["level"], ts=ts,
        dedup_key=dedup_key, ttl_s=data["ttl_s"], target_hint=data["target_hint"],
        summary=data["summary"], body=data["body"])

    if not inserted:
        return {"id": event_id, "duplicate": True}
    return {"id": event_id}
