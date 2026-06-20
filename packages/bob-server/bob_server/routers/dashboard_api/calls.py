"""Dashboard API: Call detail."""

from __future__ import annotations

from fastapi import APIRouter

from bob_server.routers.dashboard_api._common import *  # noqa: F403,F405


router = APIRouter()


@router.get("/api/calls/{call_id}")
async def get_call_detail(request: Request, call_id: str) -> dict[str, Any]:
    if not _check_auth(request):
        return {"error": "unauthorized"}
    db = _db(request)

    row = await db.fetch_one(
        """SELECT id, created_at, provider, model, call_category, session_key,
                  system_prompt, user_message, messages_json, tools_json,
                  response_text, latency_seconds, ttft_seconds,
                  prompt_tokens, completion_tokens, total_tokens, cached_tokens,
                  status, error_message, tool_blocks_json
           FROM llm_call_log WHERE id = ?""",
        (call_id,),
    )
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
    }


