"""Dashboard API: Call detail."""

from __future__ import annotations

from fastapi import APIRouter

from server.routers.dashboard_api._common import *  # noqa: F403,F405


router = APIRouter()


@router.get("/api/calls/{call_id}")
async def get_call_detail(request: Request, call_id: str) -> dict[str, Any]:
    if not _check_auth(request):
        return {"error": "unauthorized"}
    db = _db(request)

    from server.repositories.llm_call_log import LlmCallLogRepository
    row = await LlmCallLogRepository(db).get(call_id)
    if not row:
        return {"error": "not found"}

    messages: list[dict[str, Any]] | None = None
    if row["messages_json"]:
        try:
            messages = json.loads(row["messages_json"])
        except (json.JSONDecodeError, TypeError):
            pass

    tool_calls: list[dict[str, Any]] | None = None
    if row["tool_blocks_json"]:
        try:
            parsed = json.loads(row["tool_blocks_json"])
            if isinstance(parsed, list):
                tool_calls = parsed
        except (json.JSONDecodeError, TypeError):
            pass

    tools: list[dict[str, Any]] | None = None
    if row["tools_json"]:
        try:
            tools = json.loads(row["tools_json"])
        except (json.JSONDecodeError, TypeError):
            pass

    return {
        "id": row["id"],
        "created_at": _utc(row["created_at"]),
        "provider": row["provider"],
        "model": row["model"],
        "call_category": row["call_category"],
        "session_key": row["session_key"],
        "status": row["status"],
        "latency_seconds": row["latency_seconds"],
        "ttft_seconds": row["ttft_seconds"],
        "prompt_tokens": row["prompt_tokens"],
        "completion_tokens": row["completion_tokens"],
        "total_tokens": row["total_tokens"],
        "cached_tokens": row["cached_tokens"],
        "messages": messages,
        "tool_calls": tool_calls,
        "tools": tools,
        "response_text": row["response_text"],
        "user_message": row["user_message"],
        "system_prompt": row["system_prompt"],
        "error_message": row["error_message"],
        "generation_id": row["generation_id"],
        "served_by": row["served_by"],
        "served_quant": row["served_quant"],
        "reasoning_effort": row["reasoning_effort"],
    }


@router.get("/api/calls/{call_id}/trace")
async def get_call_trace(request: Request, call_id: str) -> dict[str, Any]:
    """The durable per-round timeline for a call (2026-10-03 trace uplift):
    reasoning parts (summary + raw dialects), tool calls/results, round
    boundaries with latency and tokens — written live while the turn runs,
    so this doubles as the live view's backfill/replay source."""
    if not _check_auth(request):
        return {"error": "unauthorized"}
    db = _db(request)

    from server.repositories.llm_call_log import LlmCallLogRepository
    from server.repositories.llm_trace import LlmTraceRepository
    row = await LlmCallLogRepository(db).get(call_id)
    if not row:
        return {"error": "not found"}
    events = await LlmTraceRepository(db).for_call(call_id)
    return {
        "call_id": call_id,
        "status": row["status"],
        "model": row["model"],
        "session_key": row["session_key"],
        "events": events,
    }


