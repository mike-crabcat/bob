"""Backburner — detach slow dispatch turns to the background.

Design + rationale: docs/detach-v2.md (v1 history: docs/backburner-plan.md).

- DispatchRunner races the main LLM call against a wall-clock watchdog
  (``detach_after_seconds``). On timeout for a WHATSAPP_INCOMING turn,
  ``detach_probe`` inspects the in-flight transcript (the running
  llm_call_log row's ``messages_json``) and produces a one-line summary +
  a holding ack in Bob's voice.
- The turn row completes (answered by the holding ack), the work is
  registered as a ``detached_turn`` subagent + goal (register, never
  replay), a transcript placeholder announces the flight, and the holding
  ack goes out through the effects outbox — the session lock releases.
- v2: the flight keeps speaking DIRECTLY, attributed (``spec.flight``).
  There is no capture mode and no relay turn: what the flight sends was
  sent, and its messages land in history under ``[bg <id>]``. Only a
  flight that finishes without ever sending gets a non-imperative
  fallback wake. The v1 "capture + relay" design lied about side effects
  ("nothing was delivered — deliver it") and re-executed work three times
  live (2026-08-30, 2026-09-10 double-sell, 2026-09-11 double-gif).
- A supervisor coroutine owns all terminal bookkeeping: spoke-completions
  settle quietly, silent completions get the fallback wake, a user kill
  settles quietly, failure/deadline wake honestly.

Failure philosophy (the tier-2 probe contract): probe infrastructure must
never block or break a dispatch — every probe failure degrades to
templates, and a detach failure degrades to waiting for the turn inline.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any
from uuid import uuid4

from server.services.base import BaseService, utcnow

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

# dispatch_id -> armed flight dict, so note_tool_call (fed by the dispatch
# layer's per-call callback) can count executed tool calls against the
# detached run. A flight that finishes silent AND zero-call narrated work
# that never happened (2026-09-17 phantom sweeper build) — the relay must
# not vouch for it. Armed at detach, dropped in the supervisor's finally.
_flight_by_dispatch: dict[str, dict] = {}


def note_tool_call(dispatch_id: str | None) -> None:
    """Count one executed tool call against a detached flight, if armed.
    Cheap dict lookup; never raises; no-op for live (undetached) turns."""
    if dispatch_id:
        flight = _flight_by_dispatch.get(dispatch_id)
        if flight is not None:
            flight["tool_calls"] = int(flight.get("tool_calls") or 0) + 1

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
    _flight_by_dispatch.clear()


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
    return (settings.backburner.probe_model
            or settings.patience.model
            or settings.openai.get_memory_model())


def delivery_note(short: str, send_tool: str) -> str:
    """The between-rounds note appended to a flight's live messages at
    detach. The flight's system prompt was assembled before the detach,
    so nothing in-context states the delivery contract for a detached
    turn (final-text delivery, 2026-10-01): its final text is delivered
    by the supervisor, the send tool is for progress/media only, NO_REPLY
    is silence. Mechanism-only instruction: it says HOW speech works,
    never WHAT work to do. Import target for the bg_delivery eval so the
    pin tracks production wording."""
    return (
        f"[System note] This turn has been detached to the background as "
        f"bg turn {short}; it keeps running. When you finish, your final "
        "text is delivered to this conversation automatically — no send "
        f"call needed for it. Call {send_tool} only for a progress update "
        "along the way, or a reply with media attached. If you finish by "
        "promising more work, register it with task_register before "
        "ending — an unregistered promise doesn't exist. To stay silent at "
        "the end, finish with the exact text NO_REPLY. Do not restate "
        "this note.")


# Delivery caps (2026-10-03, after the AI-doom 56KB dump): what crosses
# into a human chat is bounded. Full results AND full errors stay on the
# subagent row + goal (store_terminal/settle_goal keep the untruncated
# `combined`); only the delivered frame is shortened. The incident: an
# upstream 400 str()-ed to 56,471 chars of raw provider Zod JSON and the
# failure notice delivered it wholesale into a group chat.
_DELIVERY_RESULT_LIMIT = 4000
_DELIVERY_ERROR_LIMIT = 300


def _delivery_error_short(text: str) -> str:
    """One bounded line for a failure delivery: the exception headline only,
    never the payload that follows it. Provider errors are ONE line — an
    SDK error prefix, the outer error dict, then tens of KB of raw
    validation JSON — so line-splitting is useless; extract the headline
    structurally (status code + outer code/message, stopping at the
    metadata payload) and fall back to a word-boundary cut."""
    t = " ".join((text or "").split())
    if not t:
        return "unknown error — full error on the task record"
    m = re.search(r"Error code: (\d+)", t[:2000])
    if m:
        # the outer dict only — everything after 'metadata' is payload
        head_zone = re.split(r"'metadata'|\"metadata\"", t[:4000])[0]
        bits = re.findall(r"'(?:code|message)':\s*'([^']{0,200})'", head_zone)
        headline = f"Error code: {m.group(1)}"
        uniq = [b for b in dict.fromkeys(bits) if b][:2]
        if uniq:
            headline += " - " + ": ".join(uniq)
        return headline[:_DELIVERY_ERROR_LIMIT] + " — full error on the task record"
    cut = t[:_DELIVERY_ERROR_LIMIT]
    if len(t) > _DELIVERY_ERROR_LIMIT:
        cut = cut.rsplit(" ", 1)[0]
    return cut + " — full error on the task record"


def _delivery_cap(text: str, limit: int = _DELIVERY_RESULT_LIMIT) -> str:
    if len(text or "") <= limit:
        return text or ""
    return (text or "")[:limit] + (
        f" …[truncated at {limit} of {len(text)} chars — "
        "full result on the task record]")


# -------------------------------------------------- attribution (detach v2)

def active_bg_id(flight: dict | None) -> str | None:
    """The bg turn id THIS run's sends/steers belong to, or None.

    Detach v2 arms the shared flight dict with subagent_id; the send
    wrapper, steer, and group-send tools consult it to attribute output to
    the task. But the dict outlives the detached run: the attention
    coordinator's leftover sweep re-arms the flown spec's closure for
    messages that arrived mid-turn, and that re-run shares the dict — so
    the 2026-09-16 AI doom incident saw a live group turn's reply stamped
    as task 9e69b321. The fix: detach also records the llm_task
    (detach_task), and attribution applies only when the CURRENT asyncio
    task is that task (tool handlers are awaited inline by chat_with_tools,
    so a flight's sends run inside its own task). Flights without a token
    (in-flight rows from before the deploy) keep the old behaviour."""
    if not flight:
        return None
    bg = flight.get("subagent_id")
    if not bg:
        return None
    import asyncio
    token = flight.get("detach_task")
    if token is not None and token is not asyncio.current_task():
        return None
    return bg


def spec_detached(spec: Any) -> bool:
    """True when this spec's flight was armed by a detach — a re-fly (the
    coordinator's leftover sweep) must build a fresh spec instead of
    reusing it, or the new turn inherits the bg attribution and the shared
    send-seq/sent_texts state."""
    return bool((getattr(spec, "flight", None) or {}).get("subagent_id"))


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
    """Compact rendering of the current turn's half of an in-flight
    chat_with_tools messages array.

    The probe sees the triggering user message plus this turn's own tool
    activity — never the system prompt, never earlier turns' history (plan:
    detach_probe input column). The boundary is the last user message: the
    array carries prior-turn chat history AND prior-turn tool items, and a
    slow first response must not make the probe summarise stale work (found
    live 2026-08-30: a group turn 32s in with zero tool calls of its own
    showed the probe a full tail of the previous turns' merch/257 activity).
    Handles both shapes the array contains: chat messages (``role``) and
    Responses-API items (``type`` function_call / function_call_output).
    """
    from server.services.llm_dispatch import _truncate_str

    try:
        items = json.loads(messages_json or "[]")
    except (json.JSONDecodeError, TypeError):
        return ""
    if not isinstance(items, list):
        return ""

    # The triggering message is the last user item; the turn's own work is
    # everything after it. No user item at all → unexpected shape, show nothing.
    boundary = -1
    for i, it in enumerate(items):
        if isinstance(it, dict) and it.get("role") == "user":
            boundary = i
    if boundary < 0:
        return ""

    content = items[boundary].get("content", "")
    if isinstance(content, list):
        content = " ".join(
            p.get("text", "") for p in content
            if isinstance(p, dict) and p.get("type") != "input_image")
    last_user = _truncate_str(content, item_cap)

    tail: list[str] = []
    for it in items[boundary + 1:]:
        if not isinstance(it, dict):
            continue
        itype = it.get("type")
        if itype == "function_call":
            args = _truncate_str(it.get("arguments", ""), 100)
            tail.append(f"-> calls {it.get('name', '?')}({args})")
        elif itype == "function_call_output":
            tail.append(f"   <- {_truncate_str(it.get('output', ''), item_cap)}")
        elif it.get("role") == "assistant":
            text = it.get("content", "")
            text = _truncate_str(text, 160) if isinstance(text, str) else ""
            if text.strip():
                tail.append(f"bob: {text}")
        # role == "system": never present after the boundary

    lines = [f"USER'S MESSAGE: {last_user}"]
    if tail:
        lines.append("WORK SO FAR:")
        lines.extend(tail[-max_tail:])
    else:
        lines.append("WORK SO FAR: (nothing yet — still on the first response)")
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


async def run_probe(ctx: Any, dispatch_id: str, *,
                    session_key: str | None = None, contact_id: str | None = None) -> dict[str, str]:
    """Inspect the in-flight turn and produce summary + holding ack.

    Always returns usable values — probe failure degrades to templates
    (D7). Timeboxed so a slow probe model can't stall the detach. The call
    is logged with the conversation's session_key/contact_id so probes are
    visible in the session's calls view (like attention_probe rows).
    """
    settings = ctx.settings
    fallback = {"summary": TEMPLATE_SUMMARY, "holding_text": TEMPLATE_HOLDING, "source": "template"}

    transcript = ""
    try:
        from server.repositories.llm_call_log import LlmCallLogRepository
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
        from server.services.llm_dispatch import LLMDispatchService
        probe_task = asyncio.create_task(LLMDispatchService(ctx).chat(
            [{"role": "system", "content": system}, {"role": "user", "content": user}],
            model=probe_model(settings),
            call_category="detach_probe",
            session_key=session_key,
            contact_id=contact_id,
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
        info = await run_probe(self.ctx, spec.dispatch_id,
                               session_key=spec.session_key, contact_id=spec.contact_id)
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

    async def detach(self, *, spec: Any, turn: Any, session_svc: Any,
                     llm_task: asyncio.Task, quiet: bool = False,
                     messages: list | None = None) -> bool:
        """Full detach sequence (plan §Detach sequence, steps a–g).

        Returns True when the turn was detached (caller returns from run()
        immediately — the supervisor owns the task from here). Returns False
        on any failure before the point of no return; the caller degrades to
        waiting for the turn inline.

        ``quiet`` (steer-only turns, 2026-09-06): skip the holding ack —
        nobody asked anything, so there is nobody to acknowledge. The probe
        still runs (its summary labels the goal); only step f's announcement
        is dropped, and a silent steer-born flight settles quietly at
        terminal (silence is the designed outcome for stimulus work).

        ``messages`` (2026-09-30): the flight's live message list. At the
        point of no return the delivery note is appended so the flight
        learns it now owns delivery — its system prompt was assembled
        BEFORE detach, so nothing in-context says so, and the flight
        ending in plain text is the 2026-09-29 send-skip class (work done,
        answer invisible, relay + dead-man rescue one turn late).
        """
        if spec.flight is None or spec.hold_sender is None:
            return False

        info = await run_probe(self.ctx, spec.dispatch_id,
                               session_key=spec.session_key, contact_id=spec.contact_id)

        # Race guard (live 2026-08-31, AI doom group): a finishing turn's
        # send can land while the probe runs — the reply reached the group
        # 2s before detach completed. A turn that already spoke (or has
        # finished) gains nothing from detaching — wait inline instead
        # (the rare intermediate-send turn degrades the same way, bounded
        # by max_run_seconds).
        if llm_task.done() or spec.message_was_sent[0]:
            return False

        try:
            # b. History snapshot for sends so far.
            if spec.message_was_sent[0] and spec.sent_texts:
                await session_svc.add_message(
                    spec.session_key, "assistant",
                    "\n\n".join(p for p in list(spec.sent_texts) if p.strip()),
                    channel=spec.channel, dispatch_id=spec.dispatch_id)
                # Recorded by this snapshot — advance the cursor so the
                # inline fallback (detach failed past this point) doesn't
                # re-record them in _record_history.
                spec.recorded_texts = len(spec.sent_texts)

            # c. The turn is answered — by the holding ack. Frees the claim
            #    for the next turn.
            if turn is not None:
                from server.repositories.turns import TurnRepository
                await TurnRepository(self.db).complete(turn["turn_id"])

            # d. Register (register, never replay). Steer-origin rides on
            #    the goal strategy so terminal silence rules differ.
            subagent_id, goal_id = await self._register(
                spec, info, steer_origin=quiet)
        except Exception:
            logger.exception(
                "backburner: detach failed before point-of-no-return "
                "(session=%s, dispatch=%s) — waiting inline",
                spec.session_key, spec.dispatch_id)
            return False

        # e. v2 attribution (docs/detach-v2.md): the flight keeps its tools
        #    and speaks DIRECTLY — the send wrapper (and steer/group-send)
        #    consult this dict and attribute everything to the task. There
        #    is no capture: what is sent was sent.
        spec.flight["subagent_id"] = subagent_id
        spec.flight["sent"] = False
        # Honesty ledger (2026-09-17): count executed tool calls so _terminal
        # can tell a silent-but-real flight from pure narration. Registered
        # here, fed by note_tool_call from the dispatch layer's per-call
        # callback, dropped in the supervisor's finally.
        spec.flight["tool_calls"] = 0
        _flight_by_dispatch[spec.dispatch_id] = spec.flight
        # Attribution token (2026-09-17): only THIS llm_task's sends belong
        # to the task. The attention coordinator's leftover sweep can re-fly
        # the same spec for mid-turn arrivals, and that re-run shares the
        # dict — active_bg_id() compares the token against the running
        # asyncio task so the later turn keeps the live voice (AI doom
        # 2026-09-16: the "Mike — flag" reply went out as task 9e69b321).
        spec.flight["detach_task"] = llm_task

        # e2. The transcript placeholder: later turns in this conversation
        #     see the flight exists and neither wait for it nor redo it.
        try:
            from server.services.session_service import SessionService
            await SessionService(self.ctx).add_message(
                spec.session_key, "user",
                f"[bg turn {subagent_id[:8]} detached: {info['summary']} "
                "Its messages will appear under that id until it finishes. "
                "Do not take over its work — it speaks for itself.]",
                channel=spec.channel, provenance="bg_placeholder",
                dispatched=1, dispatch_id=spec.dispatch_id)
        except Exception:
            logger.warning("backburner: placeholder write failed for %s",
                           subagent_id[:8], exc_info=True)

        # f. Holding ack — skipped when the turn already spoke (D5) or the
        #    turn is steer-only (nobody to acknowledge); a send failure logs
        #    and continues (the ack is best-effort).
        if not spec.message_was_sent[0] and not quiet:
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

        # e3. Delivery note (final-text delivery, 2026-10-01): the flight's
        #     prompt predates the detach, so nothing in-context states the
        #     detached delivery contract — final text delivered by the
        #     supervisor, send tool for progress/media only, NO_REPLY for
        #     silence. Appended between rounds — the in-flight generation is
        #     untouched and the note rides into the next round's input
        #     (flights typically have many tool rounds left; a flight whose
        #     current generation is its last simply misses the note, and
        #     terminal delivery delivers whatever it wrote anyway).
        #     Best-effort like every probe-side write.
        if messages is not None:
            try:
                messages.append({"role": "user", "content": delivery_note(
                    subagent_id[:8], spec.send_tool_name
                    or "send_whatsapp_message")})
            except Exception:
                logger.warning(
                    "backburner: delivery-note append failed for %s",
                    subagent_id[:8], exc_info=True)

        self._spawn_supervisor(subagent_id, goal_id, spec, llm_task)
        logger.info(
            "backburner: detached turn %s (session=%s, dispatch=%s, probe=%s)",
            subagent_id[:8], spec.session_key, spec.dispatch_id, info["source"])
        return True

    async def _register(self, spec: Any, info: dict[str, str],
                        *, steer_origin: bool = False) -> tuple[str, str]:
        """subagents row (agent_type detached_turn) + goal held by the parent
        conversation, so goals_block carries the work into later turns."""
        from server.repositories.subagents import SubagentRepository
        from server.services.goal_service import create_goal

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

        strategy = {
            "v": 2,
            "plan": summary,
            "known": [],
            "open_questions": [],
            "next_actions": [{"action": "finish the work; post the result "
                                        "to the channel yourself (you speak "
                                        "directly, attributed)", "due": ""}],
            "refs": {"entities": [], "claims": []},
        }
        if steer_origin:
            # The relay wake this goal eventually produces is labelled
            # steer_relay (silence-ok, rescue-exempt) — see _terminal.
            strategy["steer_origin"] = True
        goal = await create_goal(
            self.ctx,
            conversation_id=sub_key,
            objective=f"[task {short}] {summary}",
            origin_conversation_id=spec.session_key,
            kind="subagent",
            strategy=strategy,
            external_ref=subagent_id,
        )
        return subagent_id, str(goal["id"])

    # ------------------------------------------------------ terminal content
    # The v2 fallback/narration-only relay builders were RETIRED with
    # final-text delivery (2026-10-01, docs/final-text-delivery-plan.md):
    # silent-flight results are delivered directly by the supervisor
    # under a '(background result…)' header (unverified variant for
    # zero-tool flights — the 2026-09-17 phantom-build withdrawal lives
    # in that header now). relay_payload still parses the historical
    # wake shapes from pre-retirement rows. _failed_content survives for
    # the one path that still wakes instead of delivering: boot recovery
    # (recover_orphaned_goals has no live spec to deliver through) and
    # the runtime terminal's delivery-failure fallback.

    @staticmethod
    def _failed_content(short: str, combined: str) -> str:
        return (
            f"[bg turn {short}] failed. {_delivery_error_short(combined)}\n\n"
            "Its tool calls before failing may have had real effects — "
            "check the current state before retrying anything. Tell the "
            "person in THIS chat plainly what happened — never carry "
            "reports to Mike or anyone else from here.")

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
                    subagent_id, goal_id, spec,
                    status="completed", result_text=result)
                return
            except asyncio.CancelledError:
                reason = _cancel_reasons.pop(spec.dispatch_id, None) or _CANCEL_REASON_KILLED
                if "user" in reason.lower():
                    await self._terminal(
                        subagent_id, goal_id, spec,
                        status="killed", result_text="")
                else:
                    await self._terminal(
                        subagent_id, goal_id, spec,
                        status="failed",
                        result_text="the background work was stopped: it exceeded "
                                    "its wall-clock budget")
                return
            except Exception as exc:
                await self._terminal(
                    subagent_id, goal_id, spec,
                    status="failed", result_text=f"the background work failed: {exc}")
                return
        except Exception:
            logger.exception("backburner: supervisor error for %s", subagent_id[:8])
        finally:
            _tasks.pop(subagent_id, None)
            _dispatch_ids.pop(subagent_id, None)
            _flight_by_dispatch.pop(spec.dispatch_id, None)

    async def _deliver_result(self, spec: Any, framed: str) -> bool:
        """Final-text delivery for a silent flight (docs/
        final-text-delivery-plan.md): deliver the result directly through
        the flight's own send tool handler — idempotency keys, send
        records and the effects outbox all apply, exactly like the
        dispatch runner's in-turn delivery. Framed as the runner speaking
        (the '(background result…)' header), never as Bob's own composed
        words — the dead-man switch's 2026-09-06 framing rule. Returns
        False when delivery was impossible (no send tool / handler
        error) — callers then settle with the result stored on the goal."""
        if not getattr(spec, "send_tool_name", ""):
            return False
        send_tool = next(
            (t for t in (spec.tools or [])
             if getattr(t, "name", "") == spec.send_tool_name), None)
        if send_tool is None:
            return False
        try:
            await send_tool.handler(framed)
            # Record the delivery in history: run() returned at detach, so
            # no _record_history will ever run for this send — without this
            # the message exists on WhatsApp but not in the transcript,
            # future-turn context (delivered_only), or search. Found on the
            # first live firing (2026-10-02 aus-legal announcement).
            from server.services.session_service import SessionService
            await SessionService(self.ctx).add_message(
                spec.session_key, "assistant", framed,
                channel=getattr(spec, "channel", None) or "whatsapp")
            return True
        except Exception:
            logger.exception(
                "backburner: terminal delivery failed via %s",
                spec.send_tool_name)
            return False

    async def _terminal(self, subagent_id: str, goal_id: str,
                        spec: Any, *, status: str,
                        result_text: str) -> None:
        """Terminal transition (detach v2 + final-text delivery 2026-10-01):
        subagent row + goal settle. Flights that spoke are done — their
        attributed messages WERE the output. A silent completion with a
        substantive result is DELIVERED directly by the supervisor through
        the send tool (no relay turn: the 2026-09-29 send-skip class cost
        a full relay + dead-man rescue per incident). Zero-tool flights
        deliver under an unverified header — the 2026-09-17 phantom-build
        harm was the system vouching; the header un-vouches. Steer-born
        flights settle quietly on every silent terminal state: nobody
        asked for the work. Never raises."""
        from server.services.dispatch_runner import is_no_reply
        from server.repositories.subagents import SubagentRepository
        from server.services.goal_service import settle_goal

        short = subagent_id[:8]
        flight = spec.flight or {}
        teed = [t for t in (flight.get("texts") or []) if t.strip()]
        spoke = bool(flight.get("sent"))
        made_tool_calls = bool(flight.get("tool_calls"))
        combined = (result_text or "").strip()
        if teed:
            combined = (combined + "\n\n(posted directly: "
                        + "\n\n".join(teed) + ")").strip()
        now = utcnow().isoformat()
        steer_origin = await self._goal_is_steer_born(goal_id)

        try:
            if status == "completed":
                await SubagentRepository(self.db).store_terminal(
                    subagent_id, status="completed",
                    result=combined or "(finished with no output)", now_iso=now)
                quiet = (spoke or (not combined) or is_no_reply(combined)
                         or steer_origin)
                if quiet:
                    # Spoke: its attributed messages were the output; waking
                    # a fresh turn to say it again is the v1 duplicate shape.
                    # Silent + steer-born: nobody asked; silence is designed.
                    await settle_goal(self.ctx, goal_id, status="completed",
                                      result=combined or
                                      "completed with no user-facing output",
                                      wake_origin=False)
                else:
                    # Silent completion with a result: deliver it here —
                    # the flight's stimulus was a human question and the
                    # holding ack promised an answer, so this is the same
                    # final-text delivery the runner applies to any
                    # human-stimulus turn. Framing depends on whether the
                    # flight actually ran anything.
                    header = (
                        f"(background result from bg turn {short})"
                        if made_tool_calls
                        else f"(background result from bg turn {short} — "
                        "UNVERIFIED: this turn ran no tools, so claims "
                        "below that work was started, sent, or finished "
                        "may not have happened; if it matters, it still "
                        "needs doing)")
                    delivered = await self._deliver_result(
                        spec, f"{header}\n\n{_delivery_cap(combined)}")
                    if not delivered:
                        logger.error(
                            "backburner: silent-flight result undeliverable "
                            "for %s — stored on the goal only", short)
                    await settle_goal(self.ctx, goal_id, status="completed",
                                      result=combined,
                                      wake_origin=False)
            elif status == "killed":
                await SubagentRepository(self.db).store_terminal(
                    subagent_id, status="killed",
                    result=combined or "(killed before finishing)", now_iso=now)
                await settle_goal(self.ctx, goal_id, status="cancelled",
                                  result="background turn killed by the user",
                                  wake_origin=False)
            else:  # failed
                await SubagentRepository(self.db).store_terminal(
                    subagent_id, status="failed",
                    result=combined or "(failed)", now_iso=now,
                    error=combined[:500])
                if steer_origin:
                    await settle_goal(self.ctx, goal_id, status="failed",
                                      result="steer-born background turn failed",
                                      wake_origin=False)
                else:
                    framed = (
                        f"(bg turn {short} failed — its earlier tool calls "
                        "may have had real effects; check the current state "
                        f"before retrying anything)\n\n"
                        + _delivery_error_short(combined))
                    delivered = await self._deliver_result(spec, framed)
                    if not delivered:
                        content = self._failed_content(short, combined)
                        await settle_goal(self.ctx, goal_id, status="failed",
                                          result=content, wake_content=content,
                                          wake_provenance="task_relay")
                    else:
                        await settle_goal(self.ctx, goal_id, status="failed",
                                          result=combined, wake_origin=False)
            logger.info("backburner: task %s -> %s%s", short, status,
                        " (spoke; no wake)" if status == "completed" and spoke
                        else "")
        except Exception:
            logger.exception("backburner: terminal bookkeeping failed for %s", short)

    async def _goal_is_steer_born(self, goal_id: str) -> bool:
        """True when this detached goal came from a steer-only turn (the
        strategy flag _register sets). False on any lookup/parsing failure —
        unknown origin keeps the speak-expected task_relay semantics."""
        from server.repositories.goals import GoalRepository
        try:
            goal = await GoalRepository(self.db).get(goal_id)
            if not goal:
                return False
            raw = goal.get("strategy_json") or "{}"
            strategy = json.loads(raw) if isinstance(raw, str) else (raw or {})
            return bool(strategy.get("steer_origin"))
        except Exception:
            return False

    # ----------------------------------------------------------- recovery

    async def recover_orphaned_goals(self) -> int:
        """Restart recovery: detached tasks died with the process (their
        subagent rows were failed by cleanup_stale), but their goals would
        ride every prompt forever. Settle them and wake the conversation to
        own the loss. Idempotent — settle_goal's CAS only moves active goals,
        so previously-settled rows are skipped."""
        from server.repositories.goals import GoalRepository
        from server.repositories.subagents import SubagentRepository
        from server.services.goal_service import settle_goal

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
            try:
                raw = goal.get("strategy_json") or "{}"
                strategy = json.loads(raw) if isinstance(raw, str) else (raw or {})
                steer_origin = bool(strategy.get("steer_origin"))
            except Exception:
                steer_origin = False
            if steer_origin:
                # A steer-born orphan waking the session to apologise is
                # uninvited speech — nobody asked for the work. Settle quietly.
                if await settle_goal(self.ctx, goal["id"], status="failed",
                                     result="steer-born background turn lost on restart",
                                     wake_origin=False):
                    moved += 1
                continue
            content = (
                f"[bg turn {subagent_id[:8]}] I lost this background turn "
                f"when I restarted — it was: {goal['objective']}. "
                "Tell the user it was interrupted and ask whether to redo it.")
            try:
                if await settle_goal(self.ctx, goal["id"], status="failed",
                                     result=content, wake_content=content,
                                     wake_provenance="task_relay"):
                    moved += 1
            except Exception:
                logger.warning("backburner: orphan settle failed for %s",
                               subagent_id[:8], exc_info=True)
        if moved:
            logger.info("backburner: restart recovery settled %d orphaned goal(s)", moved)
        return moved
