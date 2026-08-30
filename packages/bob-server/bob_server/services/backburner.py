"""Backburner — detach slow dispatch turns to the background.

Design + rationale: docs/backburner-plan.md. Summary:

- DispatchRunner races the main LLM call against a wall-clock watchdog
  (``detach_after_seconds``). On timeout for a WHATSAPP_INCOMING DM turn,
  ``detach_probe`` inspects the in-flight transcript (the running
  llm_call_log row's ``messages_json``) and produces a one-line summary +
  a holding ack in Bob's voice.
- The turn row completes (answered by the holding ack), the work is
  registered as a ``detached_turn`` subagent + goal (register, never
  replay), the send tool flips to capture mode (the detached run never
  delivers directly), the holding ack goes out through the effects outbox,
  and the session lock releases — new messages get normal turns that see
  the goal via goals_block.
- A supervisor coroutine owns all terminal bookkeeping: completion settles
  the goal (whose wake delivers the result as a new turn), a user kill
  cancels the task and settles quietly, failure/deadline settle failed
  and wake.

Failure philosophy (the tier-2 probe contract): probe infrastructure must
never block or break a dispatch — every probe failure degrades to
templates, and a detach failure degrades to waiting for the turn inline.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any
from uuid import uuid4

from bob_server.services.base import BaseService, utcnow

logger = logging.getLogger(__name__)

MODES = ("off", "shadow", "hold", "full")

# The detached chat_with_tools task per subagent — kill_subagent reaches the
# in-process task through this registry (same idea as subagent_service's
# _running_tasks, owned here so the detach lifecycle is one module).
_tasks: dict[str, asyncio.Task] = {}

# Strong refs to supervisor coroutines: asyncio keeps only weak refs, and a
# GC'd supervisor dies silently dropping the result (wake_service pattern,
# see the 2026-08-25 task-GC incident).
_supervisors: set[asyncio.Task] = set()

# dispatch_id -> human cancel reason. Written by kill_subagent / the
# supervisor deadline, read (peek) by llm_dispatch when it logs a
# CancelledError so user kills stop showing as "server restart".
_cancel_reasons: dict[str, str] = {}

# subagent_id -> dispatch_id, so kill_subagent can stamp the cancel reason
# without a schema change; the supervisor pops both on termination.
_dispatch_ids: dict[str, str] = {}

TEMPLATE_SUMMARY = "working on the sender's last request"
TEMPLATE_HOLDING = "still working on that — I'll get back to you soon"

_CANCEL_REASON_KILLED = "killed by user"
_CANCEL_REASON_DEADLINE = "detached turn exceeded its wall-clock budget"


def reset_for_tests() -> None:
    _tasks.clear()
    for sup in _supervisors:
        sup.cancel()
    _supervisors.clear()
    _cancel_reasons.clear()
    _dispatch_ids.clear()


# ------------------------------------------------------------------ gating

def mode(settings: Any) -> str:
    """Validated mode; anything unknown reads as off (never guess toward
    detaching — mirrors the enum-defensiveness rule)."""
    m = (getattr(settings.backburner, "mode", "off") or "").strip().lower()
    return m if m in MODES else "off"


def applies(settings: Any, call_category: str, session_key: str) -> bool:
    """Detach candidates: WHATSAPP_INCOMING turns on any WhatsApp session
    (DMs and groups). Plan D6 scoped v1 to DMs; widened to groups 2026-08-30
    at deploy — live traffic is group-heavy (all 7 slow turns in the first
    half-hour were groups), and Mike asked for all conversations. Group
    member-change turns are a different call_category and stay excluded."""
    if mode(settings) == "off":
        return False
    if call_category != "whatsapp_incoming":
        return False
    if ":whatsapp:" not in session_key:
        return False
    allowlist = {s.strip() for s in (settings.backburner.sessions or "").split(",") if s.strip()}
    if allowlist and session_key not in allowlist:
        return False
    return True


def probe_model(settings: Any) -> str:
    return settings.backburner.probe_model or settings.patience.model


# ------------------------------------------------------- cancel plumbing

def request_cancel(*, reason: str, dispatch_id: str | None = None) -> None:
    """Record why a detached task is being cancelled (kill or deadline)."""
    if dispatch_id:
        _cancel_reasons[dispatch_id] = reason


def peek_cancel_reason(dispatch_id: str | None) -> str | None:
    if not dispatch_id:
        return None
    return _cancel_reasons.get(dispatch_id)


def pop_task(subagent_id: str) -> asyncio.Task | None:
    return _tasks.pop(subagent_id, None)


def request_kill(subagent_id: str) -> dict[str, Any]:
    """User-initiated cancel of a live detached task. The supervisor observes
    the CancelledError + reason and does all terminal bookkeeping (killed
    status, quiet goal settle, capture snapshot). Returns {"ok": bool,
    "error"?}; already-finished is an error so the model relays the result
    instead of claiming a kill (the likely 'cancel it arrived just as the
    task finished' race)."""
    task = _tasks.get(subagent_id)
    if task is None or task.done():
        return {"ok": False, "error": "task already finished"}
    dispatch_id = _dispatch_ids.get(subagent_id)
    if dispatch_id:
        _cancel_reasons[dispatch_id] = _CANCEL_REASON_KILLED
    task.cancel()
    logger.info("backburner: kill requested for %s", subagent_id[:8])
    return {"ok": True}


# ------------------------------------------------------------------ probe

def build_transcript(messages_json: str | None, *, max_tail: int = 20, item_cap: int = 240) -> str:
    """Compact rendering of an in-flight chat_with_tools messages array.

    The probe sees the triggering user message plus recent tool activity —
    never the system prompt (plan: detach_probe input column). Handles both
    shapes the array contains: chat messages (``role``) and Responses-API
    items (``type`` function_call / function_call_output).
    """
    from bob_server.services.llm_dispatch import _truncate_str

    try:
        items = json.loads(messages_json or "[]")
    except (json.JSONDecodeError, TypeError):
        return ""
    if not isinstance(items, list):
        return ""

    last_user = ""
    tail: list[str] = []
    for it in items:
        if not isinstance(it, dict):
            continue
        role = it.get("role")
        itype = it.get("type")
        if itype == "function_call":
            args = _truncate_str(it.get("arguments", ""), 100)
            tail.append(f"-> calls {it.get('name', '?')}({args})")
        elif itype == "function_call_output":
            tail.append(f"   <- {_truncate_str(it.get('output', ''), item_cap)}")
        elif role == "user":
            content = it.get("content", "")
            if isinstance(content, list):
                content = " ".join(
                    p.get("text", "") for p in content
                    if isinstance(p, dict) and p.get("type") != "input_image")
            text = _truncate_str(content, item_cap)
            if text.strip():
                last_user = text
        elif role == "assistant":
            content = it.get("content", "")
            text = _truncate_str(content, 160) if isinstance(content, str) else ""
            if text.strip():
                tail.append(f"bob: {text}")
        # role == "system": deliberately skipped

    lines = []
    if last_user:
        lines.append(f"USER'S MESSAGE: {last_user}")
    if tail:
        lines.append("WORK SO FAR:")
        lines.extend(tail[-max_tail:])
    return "\n".join(lines)


def _parse_probe_output(raw: str | None) -> dict[str, str] | None:
    """Defensively extract {"summary", "holding_text"} from the probe reply.
    Accepts bare JSON or fenced; rejects empty/oversized fields."""
    if not raw:
        return None
    text = raw.strip()
    if text.startswith("```"):
        text = text.strip("`").lstrip("json").strip()
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        obj = json.loads(text[start:end + 1])
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(obj, dict):
        return None
    summary = str(obj.get("summary") or "").strip()
    holding = str(obj.get("holding_text") or "").strip()
    if not summary or not holding:
        return None
    return {"summary": summary[:300], "holding_text": holding[:300]}


async def run_probe(ctx: Any, dispatch_id: str) -> dict[str, str]:
    """Inspect the in-flight turn and produce summary + holding ack.

    Always returns usable values — probe failure degrades to templates
    (D7). Timeboxed so a slow probe model can't stall the detach.
    """
    settings = ctx.settings
    fallback = {"summary": TEMPLATE_SUMMARY, "holding_text": TEMPLATE_HOLDING, "source": "template"}

    transcript = ""
    try:
        from bob_server.repositories.llm_call_log import LlmCallLogRepository
        row = await LlmCallLogRepository(ctx.db).get_running_by_dispatch(dispatch_id)
        if row:
            transcript = build_transcript(row.get("messages_json"))
    except Exception:
        logger.warning("backburner: probe transcript read failed", exc_info=True)

    bot = settings.patience.bot_name or "Bob"
    system = (
        f'You inspect an in-progress turn of "{bot}", an AI assistant on WhatsApp. '
        "The turn has been running for a while. Work out what it is doing and write "
        "a short holding message to send meanwhile.\n"
        'Reply with ONLY a JSON object: {"summary": "...", "holding_text": "..."}\n'
        "- summary: one sentence, third person, concrete (e.g. \"checking the hotel "
        'bookings against the calendar\").\n'
        f"- holding_text: at most 140 characters, in {bot}'s own voice (lowercase, "
        "casual, honest), acknowledging you're still on it — no emojis, no promises "
        "more specific than 'soon'.\n"
        "If the transcript is thin or unclear, keep both generic."
    )
    user = ("## In-flight turn\n"
            + (transcript or "(no tool activity recorded yet — still on the first response)"))

    try:
        from bob_server.services.llm_dispatch import LLMDispatchService
        probe_task = asyncio.create_task(LLMDispatchService(ctx).chat(
            [{"role": "system", "content": system}, {"role": "user", "content": user}],
            model=probe_model(settings),
            call_category="detach_probe",
            max_tokens=250,
            temperature=0.5,
        ))
    except Exception:
        logger.warning("backburner: probe dispatch failed", exc_info=True)
        return fallback

    done, _ = await asyncio.wait({probe_task}, timeout=settings.backburner.probe_timeout_seconds)
    if not done:
        probe_task.cancel()
        logger.warning("backburner: detach_probe timed out for %s — templates", dispatch_id)
        return fallback
    try:
        parsed = _parse_probe_output(probe_task.result())
    except Exception:
        logger.warning("backburner: detach_probe call failed — templates", exc_info=True)
        return fallback
    if parsed is None:
        logger.warning("backburner: detach_probe output unparseable — templates")
        return fallback
    return {**parsed, "source": "probe"}


# ---------------------------------------------------------------- service

class BackburnerService(BaseService):
    """Detach orchestration: probe/ack (shadow+hold modes) and the full
    detach sequence + supervisor (full mode)."""

    async def probe_and_maybe_ack(self, spec: Any, *, send_ack: bool) -> dict[str, str] | None:
        """shadow/hold modes: run the probe, log it, optionally send the
        holding ack — the turn keeps waiting inline (no detach)."""
        info = await run_probe(self.ctx, spec.dispatch_id)
        logger.info(
            "backburner[%s]: slow turn probe (session=%s, dispatch=%s) "
            "summary=%r holding=%r source=%s",
            mode(self.ctx.settings), spec.session_key, spec.dispatch_id,
            info["summary"], info["holding_text"], info["source"])
        if send_ack and not spec.message_was_sent[0] and spec.hold_sender is not None:
            try:
                await spec.hold_sender(info["holding_text"])
            except Exception:
                logger.warning("backburner: holding ack send failed (dispatch=%s)",
                               spec.dispatch_id, exc_info=True)
        return info

    async def detach(self, *, spec: Any, turn: Any, session_svc: Any, llm_task: asyncio.Task) -> bool:
        """Full detach sequence (plan §Detach sequence, steps a–g).

        Returns True when the turn was detached (caller returns from run()
        immediately — the supervisor owns the task from here). Returns False
        on any failure before the point of no return; the caller degrades to
        waiting for the turn inline.
        """
        if spec.backburner_capture is None or spec.hold_sender is None:
            return False

        info = await run_probe(self.ctx, spec.dispatch_id)

        try:
            # b. History snapshot for sends so far (D4: the detached path
            #    never writes conversation history again).
            if spec.message_was_sent[0] and spec.sent_texts:
                await session_svc.add_message(
                    spec.session_key, "assistant",
                    "\n\n".join(p for p in list(spec.sent_texts) if p.strip()),
                    channel=spec.channel, dispatch_id=spec.dispatch_id)

            # c. The turn is answered — by the holding ack. Frees the claim
            #    for the next turn.
            if turn is not None:
                from bob_server.repositories.turns import TurnRepository
                await TurnRepository(self.db).complete(turn["turn_id"])

            # d. Register (register, never replay).
            subagent_id, goal_id = await self._register(spec, info)
        except Exception:
            logger.exception(
                "backburner: detach failed before point-of-no-return "
                "(session=%s, dispatch=%s) — waiting inline",
                spec.session_key, spec.dispatch_id)
            return False

        # e. Capture mode: from here the flight task's sends are captured as
        #    result, never delivered (D2).
        spec.backburner_capture["enabled"] = True

        # f. Holding ack — skipped when the turn already spoke (D5); a send
        #    failure logs and continues (the ack is best-effort).
        if not spec.message_was_sent[0]:
            try:
                await spec.hold_sender(info["holding_text"])
            except Exception:
                logger.warning("backburner: holding ack send failed (dispatch=%s)",
                               spec.dispatch_id, exc_info=True)

        # g. The dispatch event publishes now — run() returns early.
        try:
            if spec.event and self.ctx.event_bus:
                topic, payload = spec.event
                await self.ctx.event_bus.publish(topic, payload)
        except Exception:
            logger.warning("backburner: dispatch event publish failed", exc_info=True)

        _tasks[subagent_id] = llm_task
        _dispatch_ids[subagent_id] = spec.dispatch_id
        self._spawn_supervisor(subagent_id, goal_id, spec, llm_task)
        logger.info(
            "backburner: detached turn %s (session=%s, dispatch=%s, probe=%s)",
            subagent_id[:8], spec.session_key, spec.dispatch_id, info["source"])
        return True

    async def _register(self, spec: Any, info: dict[str, str]) -> tuple[str, str]:
        """subagents row (agent_type detached_turn) + goal held by the parent
        conversation, so goals_block carries the work into later turns."""
        from bob_server.repositories.subagents import SubagentRepository
        from bob_server.services.goal_service import create_goal

        subagent_id = str(uuid4())
        short = subagent_id[:8]
        sub_key = f"subagent:{spec.session_key}:{short}"
        now = utcnow().isoformat()
        summary = info["summary"]

        await SubagentRepository(self.db).insert(
            subagent_id=subagent_id, parent_session_key=spec.session_key,
            session_key=sub_key, task=f"[task {short}] {summary}",
            agent_type="detached_turn", persona=0, model="",
            contact_id=None, modality="", now_iso=now)
        await SubagentRepository(self.db).set_status(subagent_id, "running", now)

        goal = await create_goal(
            self.ctx,
            conversation_id=sub_key,
            objective=f"[task {short}] {summary}",
            origin_conversation_id=spec.session_key,
            kind="subagent",
            strategy={
                "v": 2,
                "plan": summary,
                "known": [],
                "open_questions": [],
                "next_actions": [{"action": "finish the work; the result is relayed to the user", "due": ""}],
                "refs": {"entities": [], "claims": []},
            },
            external_ref=subagent_id,
        )
        return subagent_id, str(goal["id"])

    # ---------------------------------------------------------- supervisor

    def _spawn_supervisor(self, subagent_id: str, goal_id: str, spec: Any,
                          llm_task: asyncio.Task) -> None:
        sup = asyncio.create_task(
            self._supervise(subagent_id, goal_id, spec, llm_task),
            name=f"backburner-supervisor:{subagent_id[:8]}")
        _supervisors.add(sup)
        sup.add_done_callback(_supervisors.discard)

    async def _supervise(self, subagent_id: str, goal_id: str, spec: Any,
                         llm_task: asyncio.Task) -> None:
        """Own the detached task to its terminal state. All bookkeeping funnels
        through _terminal; this coroutine must never raise."""
        capture = spec.backburner_capture
        max_run = self.ctx.settings.backburner.max_run_seconds
        try:
            done, _ = await asyncio.wait({llm_task}, timeout=max_run + 90.0)
            if not done:
                # Backstop for a hung call: the in-loop wall-clock limit only
                # checks between iterations.
                request_cancel(reason=_CANCEL_REASON_DEADLINE,
                               dispatch_id=spec.dispatch_id)
                llm_task.cancel()
            try:
                result = await llm_task
                await self._terminal(
                    subagent_id, goal_id, capture,
                    status="completed", result_text=result)
                return
            except asyncio.CancelledError:
                reason = _cancel_reasons.pop(spec.dispatch_id, None) or _CANCEL_REASON_KILLED
                if "user" in reason.lower():
                    await self._terminal(
                        subagent_id, goal_id, capture,
                        status="killed", result_text="")
                else:
                    await self._terminal(
                        subagent_id, goal_id, capture,
                        status="failed",
                        result_text="the background work was stopped: it exceeded "
                                    "its wall-clock budget")
                return
            except Exception as exc:
                await self._terminal(
                    subagent_id, goal_id, capture,
                    status="failed", result_text=f"the background work failed: {exc}")
                return
        except Exception:
            logger.exception("backburner: supervisor error for %s", subagent_id[:8])
        finally:
            _tasks.pop(subagent_id, None)
            _dispatch_ids.pop(subagent_id, None)

    async def _terminal(self, subagent_id: str, goal_id: str,
                        capture: dict, *, status: str, result_text: str) -> None:
        """Terminal transition: subagent row, goal settle (+ wake), captured
        sends snapshotted into the result. Never raises."""
        from bob_server.services.dispatch_runner import is_no_reply
        from bob_server.repositories.subagents import SubagentRepository
        from bob_server.services.goal_service import settle_goal

        short = subagent_id[:8]
        captured = [t for t in (capture.get("texts") or []) if t.strip()]
        combined = (result_text or "").strip()
        if captured:
            combined = (combined + "\n\n" + "\n\n".join(captured)).strip()
        now = utcnow().isoformat()

        try:
            if status == "completed":
                quiet = (not combined) or is_no_reply(combined)
                await SubagentRepository(self.db).store_terminal(
                    subagent_id, status="completed",
                    result=combined or "(finished with no output)", now_iso=now)
                if quiet:
                    # Open question 4 (plan): nothing to say — settle quietly.
                    await settle_goal(self.ctx, goal_id, status="completed",
                                      result="completed with no user-facing output",
                                      wake_origin=False)
                else:
                    content = (
                        f"[Background task {short}] {combined}\n\n"
                        "This background task has finished. Relay the result to the "
                        "user with a short summary in your own voice. If part of it "
                        "failed, say so plainly.")
                    await settle_goal(self.ctx, goal_id, status="completed",
                                      result=content, wake_content=content)
            elif status == "killed":
                await SubagentRepository(self.db).store_terminal(
                    subagent_id, status="killed",
                    result=combined or "(killed before finishing)", now_iso=now)
                await settle_goal(self.ctx, goal_id, status="cancelled",
                                  result="background task killed by the user",
                                  wake_origin=False)
            else:  # failed
                await SubagentRepository(self.db).store_terminal(
                    subagent_id, status="failed",
                    result=combined or "(failed)", now_iso=now,
                    error=combined[:500])
                content = (
                    f"[Background task {short}] {combined}\n\n"
                    "This background task failed. Tell the user plainly what happened "
                    "and decide whether it's worth retrying.")
                await settle_goal(self.ctx, goal_id, status="failed",
                                  result=content, wake_content=content)
            logger.info("backburner: task %s -> %s", short, status)
        except Exception:
            logger.exception("backburner: terminal bookkeeping failed for %s", short)

    # ----------------------------------------------------------- recovery

    async def recover_orphaned_goals(self) -> int:
        """Restart recovery: detached tasks died with the process (their
        subagent rows were failed by cleanup_stale), but their goals would
        ride every prompt forever. Settle them and wake the conversation to
        own the loss. Idempotent — settle_goal's CAS only moves active goals,
        so previously-settled rows are skipped."""
        from bob_server.repositories.goals import GoalRepository
        from bob_server.repositories.subagents import SubagentRepository
        from bob_server.services.goal_service import settle_goal

        rows = await SubagentRepository(self.db).list_by_type(
            agent_type="detached_turn", status="failed", limit=200)
        moved = 0
        for row in rows:
            subagent_id = row["id"]
            try:
                goal = await GoalRepository(self.db).get_by_external_ref(subagent_id)
            except Exception:
                goal = None
            if not goal or goal.get("status") != "active":
                continue
            content = (
                f"[Background task {subagent_id[:8]}] I lost this background task "
                f"when I restarted — it was: {goal['objective']}. "
                "Tell the user it was interrupted and ask whether to redo it.")
            try:
                if await settle_goal(self.ctx, goal["id"], status="failed",
                                     result=content, wake_content=content):
                    moved += 1
            except Exception:
                logger.warning("backburner: orphan settle failed for %s",
                               subagent_id[:8], exc_info=True)
        if moved:
            logger.info("backburner: restart recovery settled %d orphaned goal(s)", moved)
        return moved
