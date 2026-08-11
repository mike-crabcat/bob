"""Phone call result service — generates call summaries and dispatches results to originating sessions."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from bob_server.context import AppContext
from bob_server.services.thread_result_service import dispatch_thread_result

if TYPE_CHECKING:
    from bob_server.services.whatsapp_bridge_service import WhatsAppBridgeService

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

    call_row = await db.fetch_one("SELECT engine, transcript FROM phone_calls WHERE id = ?", (call_id,))
    engine = (call_row["engine"] if call_row else None) or "default"

    if engine == "openai_realtime":
        transcript = (call_row["transcript"] if call_row else "") or ""
        if not transcript.strip():
            return f"Call {status} before any conversation took place."
        return await _summarize_transcript(ctx, agenda, transcript)

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
    wa_service: WhatsAppBridgeService | None = None,
) -> None:
    """Generate a call summary and dispatch the result to the originating session."""
    summary = await generate_call_summary(ctx, call_id, agenda, status)

    result_content = (
        f"## Call Result\n"
        f"Status: {status}\n"
        f"Agenda: {agenda}\n\n"
        f"{summary}"
    )

    await dispatch_thread_result(
        ctx,
        origin_session_key=origin_session_key,
        result_content=result_content,
        call_category="call_result",
        wa_service=wa_service,
    )
