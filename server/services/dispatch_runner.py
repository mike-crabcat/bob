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

import asyncio
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

# Provenances that mark a pending message as a system nudge rather than
# inbound a human wrote. A turn claimed by ONLY these isn't expected to
# speak — its un-sent final text is internal bookkeeping (e.g. a goal-state
# fold summary), and rescuing it mails internal monologue to the chat.
# task_relay (background-task results) is deliberately NOT here: those turns
# exist to speak, so the send-tool rescue covers them.
_SILENCE_OK_PROVENANCES = {"wake_nudge"}

# Backburner (docs/backburner-plan.md): only turns with a HUMAN stimulus
# detach. Turns claimed solely by system nudges (goal folds, background-task
# relays) or routine deliveries aren't conversations — a holding ack for
# them is uninvited speech (the "Folded:…" leak class, 2026-08-29), and
# detaching relay turns amplifies: task → relay nudge → slow relay turn →
# detached task → relay nudge → … (observed in the AI doom group, 2026-08-30).
# task_relay rides here too: relays must not detach, but — unlike wake_nudge
# — they stay rescue-eligible (see _SILENCE_OK_PROVENANCES).
_DETACH_QUIET_PROVENANCES = {"wake_nudge", "routine", "task_relay"}

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
    # Backburner (docs/backburner-plan.md). capture: shared with the send
    # tool — flip to True at detach and post-detach sends are captured as
    # the task result instead of delivered. hold_sender: sends the holding
    # ack through the effects outbox; both are None on non-detachable specs.
    backburner_capture: dict | None = None
    hold_sender: Callable[[str], Awaitable[None]] | None = None
    quota_restore: bool = False
    on_quota_exhausted: Callable[[], Awaitable[None]] | None = None
    # Turn-lifecycle side-channel hooks (channel-neutral; the WhatsApp typing
    # indicator is the first consumer). on_turn_active fires when the turn's
    # LLM phase begins; on_turn_settled fires on EVERY exit after that (the
    # finally in run()). Both must be no-raise; None on specs whose channel
    # has nothing to signal (email, group events).
    on_turn_active: Callable[[], Awaitable[None]] | None = None
    on_turn_settled: Callable[[], Awaitable[None]] | None = None
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
    from server.repositories.conversations import ConversationRepository
    from server.services import model_registry
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
        from server.repositories.history import HistoryRepository
        from server.services.llm_dispatch import LLMDispatchService
        from server.services.prompt_assembler import build_chat_messages
        from server.services.session_dispatch_gate import SessionDispatchGate
        from server.services.session_service import SessionService

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
            # Rescue eligibility: a turn triggered solely by system nudges
            # isn't expected to speak. Computed from what this turn actually
            # claimed, so a nudge racing a real inbound message keeps the
            # rescue (the human message still deserves a reply).
            provenances = await history_repo.claimed_provenances(claimed_ids)
            expect_send = any(p not in _SILENCE_OK_PROVENANCES for p in provenances)
            human_stimulus = any(p not in _DETACH_QUIET_PROVENANCES for p in provenances)
            await session_svc.mark_dispatched(session_key)

            # Durable turn (Bob3 invariants 4-6): claim this conversation's
            # pending event_log events under lease. Advisory alongside the
            # session_messages claim — a dispatch with no matching events
            # (e.g. group-event notifications) simply runs without a turn row.
            turn = None
            try:
                from server.repositories.turns import TurnRepository
                turn = await TurnRepository(self.db).claim(
                    session_key, lease_owner=_lease_owner())
            except Exception:
                logger.warning("turn claim failed for %s", session_key, exc_info=True)

            # Resolved before the messages are built so the turn-scoped model
            # line can ride the system message — the persona no longer names
            # a model (switching went live), the per-turn line does.
            model_arg = await self._resolve_model_override(session_key)

            messages = await build_chat_messages(
                None, session_key,
                db=self.db,
                system_content=spec.system_content,
                max_history=spec.max_history,
                # The claims this turn is answering: marks the new stimulus,
                # keeps system nudges from reading as human speech, and
                # re-presents the stimulus as a trailing user turn when the
                # replay ends with a prior turn's reply (see prompt_assembler).
                claimed_ids=set(claimed_ids),
                send_tool_name=spec.send_tool_name,
                current_model=model_arg or self.ctx.settings.openai.default_model,
                current_model_override=model_arg is not None,
            )
            if spec.transform_messages is not None:
                messages = spec.transform_messages(messages)

            # Turn-lease heartbeat (bug fix alongside Backburner): a slow
            # model can outrun the claim's 300s lease, after which the next
            # claim treats this live turn as a zombie and releases its
            # events mid-run. Renew every 60s; self-terminates once the
            # turn leaves 'running'.
            heartbeat_task = None
            if turn is not None:
                turn_id = turn["turn_id"]

                async def _heartbeat() -> None:
                    while True:
                        await asyncio.sleep(60)
                        try:
                            from server.repositories.turns import TurnRepository
                            alive = await TurnRepository(self.db).heartbeat_lease(turn_id)
                        except Exception:
                            logger.warning("turn heartbeat failed", exc_info=True)
                            return
                        if not alive:
                            return

                heartbeat_task = asyncio.create_task(_heartbeat(), name=f"turn-heartbeat:{turn_id[:16]}")

            # Wall-clock budget for main WhatsApp turns (bug fix): without
            # one, a hung call holds the session lock forever. Enforced
            # between iterations — the in-flight round always completes.
            is_main_turn = spec.call_category == "whatsapp_incoming"
            time_limit = (self.ctx.settings.backburner.max_run_seconds
                          if is_main_turn else None)
            # Iteration cap for the same turns: a GLM tool odyssey at ~10s a
            # call burns the whole wall-clock budget without finishing
            # (2026-09-01: three 60+-iteration turns killed at the budget).
            # Subagents cap at 30; main turns get a little more headroom.
            iteration_cap = 35 if is_main_turn else 100

            async def _llm() -> str:
                return await LLMDispatchService(self.ctx).chat_with_tools(
                    messages, spec.tools,
                    model=model_arg,
                    call_category=spec.call_category,
                    session_key=session_key,
                    dispatch_id=spec.dispatch_id,
                    contact_id=spec.contact_id,
                    time_limit_seconds=time_limit,
                    max_iterations=iteration_cap,
                )

            from server.services import backburner as backburner_mod
            bb_applies = (human_stimulus and backburner_mod.applies(
                self.ctx.settings, spec.call_category, session_key))
            bb_mode = backburner_mod.mode(self.ctx.settings)

            # Turn-active side-channel hook (typing indicator). Placement is
            # load-bearing: every statement that can raise before the LLM
            # phase sits above this line, and nothing between here and the
            # try below raises — so the finally there always clears it.
            if spec.on_turn_active is not None:
                try:
                    await spec.on_turn_active()
                except Exception:
                    logger.warning("turn-active hook failed (session=%s)",
                                   session_key, exc_info=True)

            try:
                if not bb_applies:
                    result = await _llm()
                else:
                    llm_task = asyncio.create_task(_llm(), name=f"bb-llm:{spec.dispatch_id[:12]}")
                    try:
                        _, pending = await asyncio.wait(
                            {llm_task},
                            timeout=max(0.01, self.ctx.settings.backburner.detach_after_seconds),
                        )
                    except asyncio.CancelledError:
                        if not llm_task.done():
                            llm_task.cancel()
                        raise
                    if not pending:
                        result = llm_task.result()  # may raise — handled below
                    else:
                        # Slow turn: shadow probes (log only), hold sends the
                        # holding ack and waits, full detaches (run() returns
                        # early; the supervisor owns the task).
                        bb_svc = backburner_mod.BackburnerService(self.ctx)
                        if bb_mode == "full":
                            detached = await bb_svc.detach(
                                spec=spec, turn=turn, session_svc=session_svc,
                                llm_task=llm_task)
                            if detached:
                                return ""
                            result = await llm_task
                        elif bb_mode == "hold":
                            await bb_svc.probe_and_maybe_ack(spec, send_ack=True)
                            result = await llm_task
                        else:  # shadow
                            await bb_svc.probe_and_maybe_ack(spec, send_ack=False)
                            result = await llm_task
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if turn is not None:
                    try:
                        from server.repositories.turns import TurnRepository
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
            finally:
                # Covers every exit — normal completion, the detach's early
                # return, exceptions, and cancellation. A detached task's
                # turn is already completed at this point, so the heartbeat
                # is simply reaped; it also self-terminates once a turn
                # leaves 'running'.
                if heartbeat_task is not None and not heartbeat_task.done():
                    heartbeat_task.cancel()
                # Turn-settled side-channel hook (typing indicator): every
                # exit after the active hook clears it — normal completion,
                # detach, quota, exceptions, cancellation. except Exception
                # deliberately lets CancelledError propagate unchanged.
                if spec.on_turn_settled is not None:
                    try:
                        await spec.on_turn_settled()
                    except Exception:
                        logger.warning("turn-settled hook failed (session=%s)",
                                       session_key, exc_info=True)

            # Rescue: the turn wrote a reply but never called its send tool.
            # By the prompt contract ("your text output will NOT be sent…
            # you MUST call this tool"), final-round text with no call is a
            # model-compliance failure, never an intent — patience-gated
            # silence goes through the tool as NO_REPLY, which sets the sent
            # flag. EXCEPTION: turns claimed by system nudges only
            # (expect_send=False) may legitimately end with un-sent text —
            # e.g. a goal-state fold summary — so they are not rescued.
            # Deliver through the send tool itself, reusing its NO_REPLY
            # semantics, citation stripping, and effects-outbox idempotency.
            # (The old tap — a reminder retry — managed 0/20 and was removed.)
            if (not spec.message_was_sent[0]
                    and spec.send_tool_name
                    and spec.call_category in _SEND_RESCUE_CATEGORIES
                    and expect_send
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
            elif (spec.call_category in _SEND_RESCUE_CATEGORIES
                    and not spec.message_was_sent[0] and result.strip()
                    and not is_no_reply(result) and not expect_send):
                # Internal by policy — but a substantial un-sent reply that
                # nobody will ever see is worth a trace in the journal: the
                # 2026-08-30 relay drop was invisible at debug level.
                if len(result.strip()) >= 200:
                    logger.warning(
                        "send-tool rescue skipped: nudge-only turn dropped "
                        "%d chars of un-sent text (session=%s, dispatch=%s, "
                        "head=%r)",
                        len(result.strip()), session_key, spec.dispatch_id,
                        result.strip()[:120])
                else:
                    logger.debug(
                        "send-tool rescue skipped: nudge-only turn, un-sent text "
                        "is internal (session=%s, dispatch=%s)",
                        session_key, spec.dispatch_id)

            # Relay dead-man switch (live 2026-09-03, AI doom): a task_relay
            # wake exists to deliver a background result, but the relay turn
            # can still end with nothing delivered — it NO_REPLIED off a
            # misread ("that reply already went out", per the result's own
            # captured-send summary). A NO_REPLY tool call sets the sent
            # flag without delivering, and delivered_only history records
            # nothing, so the result evaporated silently. A relay-claimed
            # turn that delivered nothing gets its payload sent by the
            # runner: silence on a must-speak turn is a misread, never
            # intent (the supervisor only wakes non-quiet results).
            if ("task_relay" in provenances
                    and not spec.sent_texts
                    and spec.send_tool_name):
                payload = await history_repo.relay_payload(claimed_ids)
                if payload:
                    send_tool = next(
                        (t for t in spec.tools if t.name == spec.send_tool_name), None)
                    if send_tool is not None:
                        try:
                            await send_tool.handler(payload)
                            logger.warning(
                                "relay dead-man rescue: turn produced no reply — "
                                "delivered the background-task payload directly "
                                "(session=%s, dispatch=%s)",
                                session_key, spec.dispatch_id)
                        except Exception:
                            logger.exception(
                                "relay dead-man rescue failed (session=%s, "
                                "dispatch=%s)", session_key, spec.dispatch_id)

            await self._record_history(spec, session_svc, result)

            if turn is not None:
                try:
                    from server.repositories.turns import TurnRepository
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
