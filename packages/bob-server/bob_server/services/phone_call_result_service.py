"""Phone call result service — call summaries relayed via the wake path (Bob3 Phase V)."""

from __future__ import annotations

import json
import logging
from typing import Any

from bob_server.context import AppContext

logger = logging.getLogger(__name__)


async def generate_call_summary(
    ctx: AppContext,
    call_id: str,
    agenda: str,
    status: str,
) -> str:
    """Generate a summary of a phone call from its transcript.

    Summarises the assistant-side transcript captured by the OpenAI Realtime
    bridge (``phone_calls.transcript``) plus any structured outcome.
    """
    db = ctx.db

    from bob_server.repositories.phone_calls import PhoneCallRepository
    call_row = await PhoneCallRepository(db).get(call_id)
    engine = (call_row["engine"] if call_row else None) or "default"

    if engine == "openai_realtime":
        transcript = (call_row["transcript"] if call_row else "") or ""
        outcome = None
        if call_row and call_row["outcome"]:
            import json
            try:
                outcome = json.loads(call_row["outcome"])
            except (json.JSONDecodeError, TypeError):
                outcome = None

        from bob_server.services.voice_dispatch_service import format_outcome
        outcome_block = format_outcome(outcome)

        if not transcript.strip() and not outcome_block:
            return f"Call {status} before any conversation took place."
        blocks = []
        if outcome_block:
            blocks.append(f"Reported outcome:\n{outcome_block}")
        if transcript.strip():
            blocks.append(f"Transcript:\n{transcript}")
        return await _summarize_transcript(ctx, agenda, "\n\n".join(blocks))

    # Legacy default-pipeline calls (phone_call_exchanges was dropped): no
    # per-exchange transcript exists, so all we can report is the status.
    return f"Call {status}. No transcript available."


async def _summarize_transcript(ctx: AppContext, agenda: str, transcript: str) -> str:
    """Run the LLM summariser over a transcript block."""
    from bob_server.services.llm_dispatch import LLMDispatchService

    messages = [
        {
            "role": "system",
            "content": (
                "Summarize this phone call transcript. Focus on what was learned or decided "
                "relative to the agenda. Be concise (2-4 sentences). "
                "If the agenda was not achieved, say so."
            ),
        },
        {
            "role": "user",
            "content": f"Agenda: {agenda}\n\nTranscript:\n{transcript}",
        },
    ]

    dispatch = LLMDispatchService(ctx)
    summary = await dispatch.chat(
        messages,
        call_category="call_summary",
        session_key=None,
    )

    return summary.strip()


async def dispatch_call_result(
    ctx: AppContext,
    *,
    call_id: str,
    origin_session_key: str,
    agenda: str,
    status: str,
    wa_service: Any | None = None,
) -> None:
    """Generate a call summary and relay it by waking the origin conversation.

    If the call was placed by a subagent with a linked goal, the goal is
    settled without a second wake (the wake below carries the result).
    ``wa_service`` is accepted for backward compatibility and unused.
    """
    from bob_server.services.wake_service import wake_conversation

    summary = await generate_call_summary(ctx, call_id, agenda, status)

    result_content = (
        f"## Call Result\n"
        f"Status: {status}\n"
        f"Agenda: {agenda}\n\n"
        f"{summary}"
    )

    woke_via_goal = await _settle_call_goal(ctx, call_id, status, result_content)
    await _append_call_event(ctx, call_id, origin_session_key, status)
    if not woke_via_goal:
        # No linked goal (or it was already settled): relay directly. When a
        # goal settled, the wake rode the settle chokepoint (goal_result /
        # call_result content, or the hierarchy roll-up for child goals).
        await wake_conversation(
            ctx, origin_session_key, result_content,
            call_category="call_result",
        )


async def _append_call_event(
    ctx: AppContext, call_id: str, origin_session_key: str, status: str,
) -> None:
    """Voice-as-binding: record the outcome as an event on the person's
    conversation (resolved via the call's subagent binding)."""
    try:
        from bob_server.services.voice_dispatch_service import (
            append_call_completed_event,
        )
        from bob_server.repositories.phone_calls import PhoneCallRepository
        row = await PhoneCallRepository(ctx.db).outcome_with_call_session(call_id)
        outcome = None
        if row and row["outcome"]:
            try:
                outcome = json.loads(row["outcome"])
            except (TypeError, ValueError):
                outcome = {"raw": row["outcome"]}
        await append_call_completed_event(
            ctx.db,
            external_id=call_id,
            call_session_key=(row["session_key"] if row else None) or "",
            origin_session_key=origin_session_key,
            status=status,
            outcome=outcome,
            duration_seconds=row["duration_seconds"] if row else None,
        )
    except Exception:
        logger.warning("failed to append call event for %s", call_id, exc_info=True)


async def _settle_call_goal(ctx: AppContext, call_id: str, status: str, result: str) -> bool:
    """Settle the goal linked to this call's subagent, if any.

    Returns True when the settle path took responsibility for relaying the
    result (origin wake via the chokepoint, or the hierarchy roll-up for a
    child goal) — the caller then skips its own direct wake."""
    try:
        from bob_server.repositories.phone_calls import PhoneCallRepository
        row = await PhoneCallRepository(ctx.db).get(call_id)
        subagent_id = row["subagent_id"] if row else None
        if not subagent_id:
            return False
        from bob_server.repositories.goals import GoalRepository
        from bob_server.services.goal_service import settle_goal

        goal = await GoalRepository(ctx.db).get_by_external_ref(subagent_id)
        if goal and goal["status"] == "active":
            outcome = "completed" if status == "completed" else "failed"
            await settle_goal(ctx, goal["id"], status=outcome, result=result,
                              wake_content=result,
                              wake_category="call_result")
            return True
    except Exception:
        logger.warning("failed to settle goal for call %s", call_id, exc_info=True)
    return False
