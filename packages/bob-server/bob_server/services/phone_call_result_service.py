"""Phone call result service — call summaries relayed via the wake path (Bob3 Phase V)."""

from __future__ import annotations

import logging
from typing import Any

from bob_server.context import AppContext

logger = logging.getLogger(__name__)

_MAX_EXCHANGES = 20


async def generate_call_summary(
    ctx: AppContext,
    call_id: str,
    agenda: str,
    status: str,
) -> str:
    """Generate a summary of a phone call from its transcript.

    For default-pipeline calls, summarises the per-exchange rows in
    ``phone_call_exchanges``. For OpenAI Realtime calls, summarises the
    assistant-side transcript captured by the bridge (``phone_calls.transcript``).
    """
    db = ctx.db

    call_row = await db.fetch_one(
        "SELECT engine, transcript, outcome FROM phone_calls WHERE id = ?", (call_id,)
    )
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

    exchanges = await db.fetch_all(
        """SELECT user_transcript, assistant_transcript
           FROM phone_call_exchanges
           WHERE call_id = ?
           ORDER BY exchange_index""",
        (call_id,),
    )

    if not exchanges:
        return f"Call {status} before connecting. No conversation took place."

    # Cap to last N exchanges
    if len(exchanges) > _MAX_EXCHANGES:
        exchanges = exchanges[-_MAX_EXCHANGES:]

    transcript_lines = []
    for ex in exchanges:
        user_text = (ex["user_transcript"] or "").strip()
        assistant_text = (ex["assistant_transcript"] or "").strip()
        if user_text:
            transcript_lines.append(f"Caller: {user_text}")
        if assistant_text:
            transcript_lines.append(f"Agent: {assistant_text}")

    return await _summarize_transcript(ctx, agenda, "\n".join(transcript_lines))


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

    await _settle_call_goal(ctx, call_id, status, result_content)
    await wake_conversation(
        ctx, origin_session_key, result_content,
        call_category="call_result",
    )


async def _settle_call_goal(ctx: AppContext, call_id: str, status: str, result: str) -> None:
    """Settle the goal linked to this call's subagent, if any (wake handled
    by the caller)."""
    try:
        row = await ctx.db.fetch_one(
            "SELECT subagent_id FROM phone_calls WHERE id = ?", (call_id,))
        subagent_id = row["subagent_id"] if row else None
        if not subagent_id:
            return
        from bob_server.repositories.goals import GoalRepository
        from bob_server.services.goal_service import settle_goal

        goal = await GoalRepository(ctx.db).get_by_external_ref(subagent_id)
        if goal and goal["status"] == "active":
            outcome = "completed" if status == "completed" else "failed"
            await settle_goal(ctx, goal["id"], status=outcome, result=result,
                              wake_origin=False)
    except Exception:
        logger.warning("failed to settle goal for call %s", call_id, exc_info=True)
