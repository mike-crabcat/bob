"""Attention coordinator — live Tier 0/1/2 dispatch gating (Bob3 Phase III cutover).

Replaces the patience gate as the authority on WHEN a WhatsApp dispatch runs
and WHETHER an unaddressed group batch runs at all.

Tiers:
- Tier 0 (structural, tier0.py): addressed detection. DMs and addressed group
  messages are always ACT — no model probe can veto them.
- Tier 1 (this module): sliding debounce windows. Addressed stimuli close in a
  2.5s micro-window; unaddressed group chatter batches for 20s. Every new
  stimulus slides the timer; typing indicators extend it; a hard max-wait cap
  bounds total latency from the first pending stimulus. A window batches every
  sender's messages into ONE turn, but each message carries its own dispatch
  spec (built with that sender's trust level) — so the window remembers the
  highest-trust spec it has seen and flies that one, not whichever message
  happened to arrive last.
- Tier 2 (tier2.py): actionability probe, run ONLY at window close and ONLY
  for batches with no addressed stimulus. ACT dispatches, STAND_DOWN flushes
  without the main LLM, WAIT extends the window once then forces a decision.

Kill switch: settings/env ``BOB_ATTENTION_ALWAYS_ACT=1`` disables Tier 2 —
every window close dispatches (structural ACT-when-addressed is unaffected;
it never consults Tier 2 in the first place).

Durability: stimuli are already stored (session_messages + event_log) before
submission, so a crash during an armed window loses nothing — the recovery
sweep (`resume_pending`) re-arms dispatch for sessions with pending messages.
Decisions are recorded to attention_shadow, which since cutover is the live
audit trail rather than a shadow.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from bob_server.services.attention.tier0 import detect_addressed

logger = logging.getLogger(__name__)

WINDOW_ADDRESSED_S = 2.5     # micro-window: addressed stimuli (all DMs, addressed group)
WINDOW_GROUP_S = 20.0        # unaddressed group chatter batches this long
TYPING_EXTEND_S = 5.0        # a typing indicator pushes the close out this far
MAX_WAIT_S = 90.0            # hard cap from first pending stimulus to decision
WAIT_EXTEND_S = 30.0         # Tier 2 WAIT extends the window this much, once


def _always_act() -> bool:
    return os.environ.get("BOB_ATTENTION_ALWAYS_ACT", "") == "1"


@dataclass
class _Window:
    """Mutable per-session window state. In-memory only — stimuli themselves
    are durable in session_messages/event_log before they reach here."""
    first_at: float = 0.0
    addressed_any: bool = False
    message_count: int = 0
    timer: asyncio.TimerHandle | None = None
    wait_extended: bool = False        # Tier 2 WAIT used its one extension
    senders: set = field(default_factory=set)
    any_trusted: bool = False          # any sender in this batch is trusted
    chosen_fn: Callable[[], Awaitable[Any]] | None = None  # highest-trust spec
    _close_cb: Callable[[], None] | None = None

    def cancel_timer(self) -> None:
        if self.timer is not None:
            self.timer.cancel()
            self.timer = None


class AttentionCoordinator:
    """Module-level singleton state; instances are cheap facades over it."""

    _windows: dict[str, _Window] = {}
    _dispatching: set[str] = set()

    def __init__(self, ctx: Any):
        self.ctx = ctx

    # -- test/lifecycle hooks -------------------------------------------------

    @classmethod
    def reset_all(cls) -> None:
        for w in cls._windows.values():
            w.cancel_timer()
        cls._windows.clear()
        cls._dispatching.clear()

    # -- ingress API ----------------------------------------------------------

    async def submit(
        self,
        session_key: str,
        dispatch_fn: Callable[[], Awaitable[Any]],
        *,
        text: str,
        chat_kind: str,
        bot_name: str = "Bob",
        bot_jid: str | None = None,
        mentioned_jids: tuple[str, ...] | list[str] = (),
        reply_to_bot: bool = False,
        sender_name: str = "",
        is_trusted: bool = False,
        probe_enabled: bool = False,
        probe_model: str = "",
        event_id: str | None = None,
    ) -> None:
        """Register a message stimulus and (re)arm the session's window."""
        res = detect_addressed(
            text, bot_name=bot_name, chat_kind=chat_kind,
            bot_jid=bot_jid, mentioned_jids=mentioned_jids,
            reply_to_bot=reply_to_bot,
        )
        window_s = WINDOW_ADDRESSED_S if res.addressed else WINDOW_GROUP_S
        await self._record_decision(
            session_key=session_key, chat_kind=chat_kind,
            addressed=res.addressed, reason=res.reason,
            window_ms=int(window_s * 1000),
            decision="ACT" if res.addressed else "WAIT",
            event_id=event_id,
        )

        now = time.monotonic()
        w = self._windows.setdefault(session_key, _Window())
        if w.message_count == 0:
            w.first_at = now
            w.wait_extended = False
        w.message_count += 1
        w.addressed_any = w.addressed_any or res.addressed
        if sender_name:
            w.senders.add(sender_name)

        if session_key in self._dispatching:
            # In-flight dispatch will either claim this message (if it hasn't
            # locked yet) or the post-dispatch sweep re-arms for leftovers
            # under the flown spec (a mid-turn message cannot upgrade or
            # downgrade a turn that is already running).
            logger.info("attention: dispatch in progress for %s, buffering", session_key)
            return

        # Spec selection: the batch turns every sender's messages into one
        # LLM call, and each message's dispatch spec was built with that
        # sender's trust level. Highest trust wins — a trusted participant
        # must not lose capabilities because an untrusted member spoke after
        # them — and within a level the latest spec wins (historical
        # behaviour). Who is trusted stays visible per-message, so the model
        # still knows whose instructions carry what weight.
        if w.chosen_fn is None or is_trusted or not w.any_trusted:
            w.chosen_fn = dispatch_fn
        w.any_trusted = w.any_trusted or is_trusted

        # Addressed stimuli shrink an already-armed long window.
        effective = WINDOW_ADDRESSED_S if w.addressed_any else WINDOW_GROUP_S
        self._arm(session_key, w, effective, w.chosen_fn or dispatch_fn,
                  probe_enabled=probe_enabled, probe_model=probe_model,
                  bot_name=bot_name)

    def notify_typing(self, session_key: str, sender_name: str = "") -> None:
        """A typing indicator extends an armed window (presence-aware Tier 1)."""
        w = self._windows.get(session_key)
        if w is None or w.timer is None or w.message_count == 0:
            return
        close = getattr(w, "_close_cb", None)
        if close is None:
            return
        remaining_cap = MAX_WAIT_S - (time.monotonic() - w.first_at)
        if remaining_cap <= 0:
            return
        delay = min(TYPING_EXTEND_S, remaining_cap)
        w.cancel_timer()
        w.timer = asyncio.get_running_loop().call_later(delay, close)
        logger.info("attention: typing from %s extends window for %s by %.1fs",
                    sender_name, session_key, delay)

    async def resume_pending(self, session_key: str,
                             dispatch_fn: Callable[[], Awaitable[Any]]) -> None:
        """Recovery: arm a micro-window for a session with pending stored
        messages (post-crash sweep or post-dispatch leftovers)."""
        w = self._windows.setdefault(session_key, _Window())
        if session_key in self._dispatching or w.timer is not None:
            return
        w.first_at = time.monotonic()
        w.message_count = max(w.message_count, 1)
        w.addressed_any = True  # stored pending messages must not be re-probed into silence
        w.chosen_fn = dispatch_fn  # no fresher spec exists for a recovered batch
        self._arm(session_key, w, WINDOW_ADDRESSED_S, dispatch_fn,
                  probe_enabled=False, probe_model="", bot_name="Bob")

    # -- internals ------------------------------------------------------------

    def _arm(self, session_key: str, w: _Window, window_s: float,
             dispatch_fn: Callable[[], Awaitable[Any]], *,
             probe_enabled: bool, probe_model: str, bot_name: str) -> None:
        loop = asyncio.get_running_loop()
        w.cancel_timer()
        remaining_cap = MAX_WAIT_S - (time.monotonic() - w.first_at)
        delay = max(0.0, min(window_s, remaining_cap))

        def _close() -> None:
            w.timer = None
            asyncio.ensure_future(self._on_close(
                session_key, dispatch_fn,
                probe_enabled=probe_enabled, probe_model=probe_model,
                bot_name=bot_name,
            ))

        w._close_cb = _close  # typing extension reuses this
        w.timer = loop.call_later(delay, _close)
        logger.info("attention: window=%.1fs armed for %s (addressed=%s, msgs=%d)",
                    delay, session_key, w.addressed_any, w.message_count)

    async def _on_close(self, session_key: str,
                        dispatch_fn: Callable[[], Awaitable[Any]], *,
                        probe_enabled: bool, probe_model: str,
                        bot_name: str) -> None:
        # Fire-and-forget task (ensure_future in _arm) — never let exceptions
        # escape or they surface as "Task exception was never retrieved".
        try:
            await self._on_close_inner(
                session_key, dispatch_fn, probe_enabled=probe_enabled,
                probe_model=probe_model, bot_name=bot_name)
        except Exception:
            logger.error("attention: window-close dispatch failed for %s "
                         "(messages stay pending for the next stimulus)",
                         session_key, exc_info=True)

    async def _on_close_inner(self, session_key: str,
                              dispatch_fn: Callable[[], Awaitable[Any]], *,
                              probe_enabled: bool, probe_model: str,
                              bot_name: str) -> None:
        w = self._windows.get(session_key)
        if w is None or session_key in self._dispatching:
            return

        decision = "ACT"
        if (not w.addressed_any) and probe_enabled and not _always_act():
            from bob_server.services.attention.tier2 import probe_actionability
            decision = await probe_actionability(
                self.ctx, session_key, bot_name=bot_name, model=probe_model)
            if decision == "WAIT" and not w.wait_extended:
                w.wait_extended = True
                self._arm(session_key, w, WAIT_EXTEND_S, dispatch_fn,
                          probe_enabled=probe_enabled, probe_model=probe_model,
                          bot_name=bot_name)
                return
            if decision == "WAIT":
                decision = "ACT"  # extension exhausted: forced decision

        if decision == "STAND_DOWN":
            await self._flush_without_dispatch(session_key)
            self._reset_window(session_key)
            return

        await self._dispatch(session_key, dispatch_fn)

    async def _dispatch(self, session_key: str,
                        dispatch_fn: Callable[[], Awaitable[Any]]) -> None:
        self._dispatching.add(session_key)
        try:
            await dispatch_fn()
        finally:
            self._dispatching.discard(session_key)
            self._reset_window(session_key)
            # Mid-turn arrivals stay pending (invariant 5); give them their
            # own turn instead of stranding them until the next stimulus.
            try:
                from bob_server.repositories.history import HistoryRepository
                leftovers = await HistoryRepository(self.ctx.db).pending_user_ids(session_key)
                if leftovers:
                    logger.info("attention: %d mid-turn arrival(s) pending for %s, re-arming",
                                len(leftovers), session_key)
                    await self.resume_pending(session_key, dispatch_fn)
            except Exception:
                logger.warning("attention: leftover sweep failed for %s",
                               session_key, exc_info=True)

    async def _flush_without_dispatch(self, session_key: str) -> None:
        from bob_server.services.session_service import SessionService
        claimed = await SessionService(self.ctx).mark_dispatched(session_key)
        logger.info("attention: STAND_DOWN for %s — %d message(s) flushed without main LLM",
                    session_key, claimed)

    def _reset_window(self, session_key: str) -> None:
        w = self._windows.get(session_key)
        if w is not None:
            w.cancel_timer()
        self._windows.pop(session_key, None)

    async def _record_decision(self, **kw: Any) -> None:
        try:
            await self.ctx.db.execute(
                """INSERT INTO attention_shadow
                   (event_id, session_key, source, chat_kind, addressed,
                    addressed_reason, proposed_window_ms, decision)
                   VALUES (?, ?, 'whatsapp', ?, ?, ?, ?, ?)""",
                (kw.get("event_id"), kw["session_key"], kw["chat_kind"],
                 1 if kw["addressed"] else 0, kw["reason"],
                 kw["window_ms"], kw["decision"]),
            )
        except Exception:
            logger.warning("attention: decision recording failed", exc_info=True)
