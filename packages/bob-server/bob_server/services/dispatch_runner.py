"""DispatchRunner — the store → lock → dispatch → deliver skeleton (Bob3 Phase II).

Replaces the five near-identical ``_run_dispatch`` closures in the WhatsApp
bridge, group-event sync, and email poller. The caller stores the inbound
message and builds tools/system content; the runner owns the shared turn
protocol:

    lock (SessionDispatchGate) → claim pending messages → build chat messages
    → LLM call → tap second-chance → record assistant history → event publish

Channel differences are explicit spec fields, not copies:
- ``quota_restore``: WhatsApp inbound restores claimed messages on LLM quota
  failure (email has no restoration — pinned asymmetry).
- ``history_policy``: 'delivered_only' (WhatsApp: only texts actually passed
  to the send tool enter history; nothing sent → nothing recorded),
  'merged_always' (email: LLM text + reply bodies, recorded even on errors),
  'merged_skip_no_reply' (group events: merged, but NO_REPLY variants with
  nothing sent are skipped).

Result-relay services are NOT replaced here (plan: Phase IV/V).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)

_NO_REPLY_VARIANTS = ("NO_REPLY", "NO REPLY", "NOTHING TO SAY")


@dataclass
class DispatchSpec:
    session_key: str
    system_content: str
    tools: list
    call_category: str
    send_tool_name: str
    dispatch_id: str
    contact_id: str | None = None
    channel: str = "whatsapp"
    max_history: int = 100
    history_policy: str = "delivered_only"  # delivered_only | merged_always | merged_skip_no_reply
    message_was_sent: list = field(default_factory=lambda: [False])
    sent_texts: list = field(default_factory=list)
    quota_restore: bool = False
    on_quota_exhausted: Callable[[], Awaitable[None]] | None = None
    transform_messages: Callable[[list], list] | None = None
    event: tuple[str, dict] | None = None  # (event_bus topic, payload)


class DispatchRunner:
    def __init__(self, ctx: Any):
        self.ctx = ctx
        self.db = ctx.db

    async def run(self, spec: DispatchSpec) -> str:
        from bob_server.repositories.history import HistoryRepository
        from bob_server.services.llm_dispatch import LLMDispatchService
        from bob_server.services.prompt_assembler import build_chat_messages
        from bob_server.services.session_dispatch_gate import SessionDispatchGate
        from bob_server.services.session_service import SessionService

        session_key = spec.session_key
        session_svc = SessionService(self.ctx)
        history_repo = HistoryRepository(self.db)

        async with SessionDispatchGate.get_lock(session_key):
            # Capture IDs of the messages we're about to claim so quota
            # failures can restore them. Without this, mark_dispatched
            # silently swallows messages that never got a reply.
            claimed_ids = await history_repo.pending_user_ids(session_key)
            if not claimed_ids:
                return ""
            await session_svc.mark_dispatched(session_key)

            messages = await build_chat_messages(
                None, session_key,
                db=self.db,
                system_content=spec.system_content,
                max_history=spec.max_history,
            )
            if spec.transform_messages is not None:
                messages = spec.transform_messages(messages)

            try:
                result = await LLMDispatchService(self.ctx).chat_with_tools(
                    messages, spec.tools,
                    call_category=spec.call_category,
                    session_key=session_key,
                    dispatch_id=spec.dispatch_id,
                    contact_id=spec.contact_id,
                )
            except Exception as exc:
                if spec.quota_restore and _is_quota_error(exc):
                    await history_repo.restore_pending(claimed_ids)
                    logger.warning(
                        "quota exhausted for %s; restored %d message(s) for retry",
                        session_key, len(claimed_ids))
                    if spec.on_quota_exhausted is not None:
                        await spec.on_quota_exhausted()
                    return ""
                raise

            # Tap: if the LLM produced text but didn't use the send tool,
            # give it a second chance with a reminder.
            if not spec.message_was_sent[0] and result.strip():
                from bob_server.services.tap import tap_dispatch, tap_enabled
                if tap_enabled():
                    result = await tap_dispatch(
                        self.ctx, messages=messages, tools=spec.tools,
                        session_key=session_key,
                        send_tool_name=spec.send_tool_name,
                        first_result=result,
                        call_category=spec.call_category,
                        dispatch_id=spec.dispatch_id,
                        contact_id=spec.contact_id,
                    )

            await self._record_history(spec, session_svc, result)

            if spec.event and self.ctx.event_bus:
                topic, payload = spec.event
                await self.ctx.event_bus.publish(topic, payload)
            return result

    async def _record_history(self, spec: DispatchSpec, session_svc: Any, result: str) -> None:
        if spec.history_policy == "delivered_only":
            # Only delivered replies belong in replayed history; raw output
            # can leak <tool_call> XML. Nothing sent → nothing recorded.
            if spec.message_was_sent[0] and spec.sent_texts:
                assistant_text = "\n\n".join(p for p in spec.sent_texts if p.strip())
                await session_svc.add_message(
                    spec.session_key, "assistant", assistant_text,
                    channel=spec.channel, dispatch_id=spec.dispatch_id)
            return

        # Merged policies: LLM text output + actually-sent bodies.
        parts = [p for p in ([result] if result.strip() else []) + spec.sent_texts
                 if p.strip()]
        assistant_text = "\n\n".join(parts) if parts else result

        if spec.history_policy == "merged_skip_no_reply":
            if not spec.message_was_sent[0] and assistant_text.strip().upper().rstrip(".") in _NO_REPLY_VARIANTS:
                return

        await session_svc.add_message(
            spec.session_key, "assistant", assistant_text,
            channel=spec.channel, dispatch_id=spec.dispatch_id)


def _is_quota_error(exc: Exception) -> bool:
    """True if the exception looks like an OpenAI insufficient-quota failure.

    openai_service wraps the SDK's RateLimitError in a RuntimeError, so we
    detect by message rather than by type.
    """
    msg = str(exc).lower()
    return "insufficient_quota" in msg or ("429" in msg and "quota" in msg)
