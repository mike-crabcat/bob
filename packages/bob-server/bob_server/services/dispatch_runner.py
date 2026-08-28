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


def is_no_reply(text: str | None) -> bool:
    """True if a silence marker appears ANYWHERE in the text.

    Exact-match let decorated variants through to the chat ('[NO_REPLY —
    Simon asked for silence…]'); containment is the rule now: any
    occurrence of a marker suppresses the whole message.
    """
    if not text:
        return False
    upper = text.upper()
    return any(v in upper for v in _NO_REPLY_VARIANTS)

# Categories where a finished turn that never called its send tool gets the
# final text delivered by the runner (see the rescue in run()). WhatsApp
# only for now: it's the measured failure (GLM-5.3 skips the final send call
# on ~20% of turns, 58% for routine-style prompts; GPT models essentially
# never do), and the email send tool doesn't pre-set its sent flag, so a
# failed email_reply would leave an error string as "final text" that the
# rescue must not mail out.
_SEND_RESCUE_CATEGORIES = {"whatsapp_incoming", "whatsapp_group_member_change"}

_LEASE_OWNER: str | None = None


def _lease_owner() -> str:
    global _LEASE_OWNER
    if _LEASE_OWNER is None:
        import os
        import uuid
        _LEASE_OWNER = f"dispatch-{os.getpid()}-{uuid.uuid4().hex[:8]}"
    return _LEASE_OWNER


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


async def resolve_session_model(db: Any, settings: Any, session_key: str) -> str | None:
    """Resolve a session's model_override policy into a model slug.

    Shared by main dispatch turns (DispatchRunner) and routine dispatches —
    both are user-visible conversation turns and follow the /model override.
    Returns None when unset or unresolvable, so the LLM call uses the global
    default and a broken override never blocks a turn. Background passes
    (memory, patience, reflection) don't call this — they keep their
    configured models by construction.
    """
    from bob_server.repositories.conversations import ConversationRepository
    from bob_server.services import model_registry
    try:
        policy = await ConversationRepository(db).get_policy(session_key)
    except Exception:
        logger.warning("model override read failed for %s", session_key, exc_info=True)
        return None
    override = (policy.get("model_override") or "").strip()
    if not override:
        return None
    slug = model_registry.resolve(override, settings.config_dir)
    if (model_registry.provider_for(slug) == model_registry.PROVIDER_OPENROUTER
            and not settings.openrouter.enabled):
        logger.warning(
            "model override %s → %s but OpenRouter is not configured; using default",
            override, slug)
        return None
    return slug


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

            # Durable turn (Bob3 invariants 4-6): claim this conversation's
            # pending event_log events under lease. Advisory alongside the
            # session_messages claim — a dispatch with no matching events
            # (e.g. group-event notifications) simply runs without a turn row.
            turn = None
            try:
                from bob_server.repositories.turns import TurnRepository
                turn = await TurnRepository(self.db).claim(
                    session_key, lease_owner=_lease_owner())
            except Exception:
                logger.warning("turn claim failed for %s", session_key, exc_info=True)

            messages = await build_chat_messages(
                None, session_key,
                db=self.db,
                system_content=spec.system_content,
                max_history=spec.max_history,
            )
            if spec.transform_messages is not None:
                messages = spec.transform_messages(messages)

            model_arg = await self._resolve_model_override(session_key)

            try:
                result = await LLMDispatchService(self.ctx).chat_with_tools(
                    messages, spec.tools,
                    model=model_arg,
                    call_category=spec.call_category,
                    session_key=session_key,
                    dispatch_id=spec.dispatch_id,
                    contact_id=spec.contact_id,
                )
            except Exception as exc:
                if turn is not None:
                    try:
                        from bob_server.repositories.turns import TurnRepository
                        await TurnRepository(self.db).fail(turn["turn_id"], str(exc))
                    except Exception:
                        logger.warning("turn fail-mark failed", exc_info=True)
                if spec.quota_restore and _is_quota_error(exc):
                    await history_repo.restore_pending(claimed_ids)
                    logger.warning(
                        "quota exhausted for %s; restored %d message(s) for retry",
                        session_key, len(claimed_ids))
                    if spec.on_quota_exhausted is not None:
                        await spec.on_quota_exhausted()
                    return ""
                raise

            # Rescue: the turn wrote a reply but never called its send tool.
            # By the prompt contract ("your text output will NOT be sent…
            # you MUST call this tool"), final-round text with no call is
            # always a model-compliance failure, never an intent — patience-
            # gated silence goes through the tool as NO_REPLY, which sets
            # the sent flag. Deliver the text through the send tool itself,
            # reusing its NO_REPLY semantics, citation stripping, and
            # effects-outbox idempotency. (The old tap — a reminder retry —
            # managed 0/20 and was removed.)
            if (not spec.message_was_sent[0]
                    and spec.send_tool_name
                    and spec.call_category in _SEND_RESCUE_CATEGORIES
                    and result.strip()
                    and not is_no_reply(result)):
                send_tool = next(
                    (t for t in spec.tools if t.name == spec.send_tool_name), None)
                if send_tool is not None:
                    try:
                        await send_tool.handler(result)
                        logger.warning(
                            "send-tool rescue: %s turn wrote a reply without "
                            "calling %s; delivered by the runner "
                            "(session=%s, dispatch=%s)",
                            spec.call_category, spec.send_tool_name,
                            session_key, spec.dispatch_id)
                    except Exception:
                        logger.exception(
                            "send-tool rescue failed (session=%s, dispatch=%s)",
                            session_key, spec.dispatch_id)

            await self._record_history(spec, session_svc, result)

            if turn is not None:
                try:
                    from bob_server.repositories.turns import TurnRepository
                    await TurnRepository(self.db).complete(turn["turn_id"])
                except Exception:
                    logger.warning("turn complete-mark failed", exc_info=True)

            if spec.event and self.ctx.event_bus:
                topic, payload = spec.event
                await self.ctx.event_bus.publish(topic, payload)
            return result

    async def _resolve_model_override(self, session_key: str) -> str | None:
        """Session model override for this dispatch turn (see resolve_session_model)."""
        return await resolve_session_model(self.db, self.ctx.settings, session_key)

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
            if not spec.message_was_sent[0] and is_no_reply(assistant_text):
                return

        await session_svc.add_message(
            spec.session_key, "assistant", assistant_text,
            channel=spec.channel, dispatch_id=spec.dispatch_id)


def _is_quota_error(exc: Exception) -> bool:
    """True if the exception looks like a provider credit/quota failure.

    openai_service wraps SDK errors in RuntimeErrors, so we detect by
    message text. Covers OpenAI (insufficient_quota) and OpenRouter
    (insufficient credits / HTTP 402) phrasings — without the OpenRouter
    patterns, an out-of-credit failure raises instead of restoring the
    claimed messages for retry.
    """
    msg = str(exc).lower()
    if "insufficient_quota" in msg or "credit_balance_exhausted" in msg:
        return True
    if "insufficient credits" in msg or "not enough credits" in msg:
        return True
    if "402" in msg and "credit" in msg:
        return True
    return "429" in msg and "quota" in msg
