"""Task registry service (docs/task-registry-plan.md) — durable promises
between conversations.

Register (any turn) → settle (any turn, script endpoint, subagent, sweep)
→ the waiter is woken with a provenance header + result. NOT an execution
engine: no scheduling, priorities or decomposition — goals own that.

Key invariants:
- settle is CAS-once (repo) and delivered via the ``task_settle`` EFFECT, so
  a crash between settle and wake replays the wake instead of losing it
  (the 2026-09-19 OOM-orphaned-Meshy class).
- every task has a due wakeup (default +1h, Mike 2026-09-19); cancelled at
  settle (payload match, the cancel_for_routine pattern).
- completions are open but provenance-stamped; the waiter's wake header
  says WHO/WHAT settled so trust stays the waiter's judgment (plan D2/D12).
- completer-side visibility: tasks_block injects pending tasks into every
  dispatch for the expected completer (plan D6 — the outreach goal's real
  trick, so the reply three hours later still knows it owes something).

"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from server.context import AppContext
from server.services.base import BaseService, iso_utc, local_minute

logger = logging.getLogger(__name__)

DEFAULT_DUE_MINUTES = 60          # Mike 2026-09-19 (Q3)
SCRIPT_GRACE_MULTIPLE = 2         # sweep: script tasks fail at 2× due
EFFECT_KIND = "task_settle"



def _close_call(task_id: str, outcome: str) -> str:
    """The exact close_goal call shape for closing a promise."""
    if outcome == "failed":
        return (f'close_goal(goal_id="{task_id}", outcome="failed", '
                'result="no <capability> in this conversation")')
    return f'close_goal(goal_id="{task_id}", result=…)'

# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

async def register_task(
    ctx: AppContext, *, waiter_session: str, title: str,
    instruction: str = "", expected_completer: str | None = None,
    due_minutes: float | None = None, due_at: str | None = None,
    refs: list[str] | None = None, source_goal_id: str | None = None,
    extra_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Create the promise + its due backstop. Optionally STEER the completer
    when one is named (the delegation half — the outreach composition)."""
    from server.repositories.tasks import TaskRepository
    from server.repositories.wakeups import WakeupRepository

    # Per-person work never delegates to a GROUP (Mike 2026-09-26, the
    # AI-doom spam root cause): a group turn can only speak in the group —
    # no DM tool — so a "chase X via DM" task steered there is an
    # unresolvable obligation that loops until its iteration cap. REFUSED
    # at registration (no row, no backstop, no steer); the caller
    # re-registers against a DM-capable session or does the outreach
    # itself. Rooms have send_whatsapp_to_contact; groups get outcomes.
    if (waiter_session.startswith("agent:goal-")
            and expected_completer
            and ":group:" in expected_completer):
        note = ("per-person work was NOT registered: the group conversation "
                "cannot DM individuals (its turns can only reply in the "
                "group). Re-register with completer_session = the person's "
                "DM conversation (agent:main:whatsapp:dm:<phone>), or do "
                "the outreach yourself with send_whatsapp_to_contact.")
        logger.warning("register_task refused room->group delegation: "
                       "waiter=%s completer=%s", waiter_session,
                       expected_completer)
        return {"ok": False, "error": note}

    due = due_at or (
        datetime.now(timezone.utc)
        + timedelta(minutes=DEFAULT_DUE_MINUTES if due_minutes is None
                    else due_minutes)).isoformat()
    task = await TaskRepository(ctx.db).create(
        title=title, waiter_session=waiter_session,
        payload={"instruction": instruction, **(extra_payload or {})},
        expected_completer=expected_completer, due=due,
        source_goal_id=source_goal_id, refs=refs or [])
    await WakeupRepository(ctx.db).schedule(
        conversation_id=waiter_session, not_before=due,
        kind="task_due", payload={"task_id": task["id"],
                                  "note": "task due backstop"})
    # Steer only conversation-shaped completers (agent:…/…:… session keys).
    # Subagent ids and 'script' are spawned WITH the task — a delegation
    # wake to them is a misdirected turn (found live in Phase 3 tests:
    # the parent's first wake went to the subagent uuid).
    steer_note = ""
    if (expected_completer and expected_completer != waiter_session
            and ":" in expected_completer
            and not expected_completer.startswith("subagent")):
        # Rooms delegating per-person work to their ORIGIN GROUP (2026-09-25:
        # figurine chases posted publicly in AI Doom): warn, don't block —
        # group completers are for group-wide outcomes only.
        if (waiter_session.startswith("agent:goal-")
                and ":group:" in expected_completer):
            steer_note = ("per-person work (offers/chases/confirmations) "
                          "belongs in that person's DM — a group completer "
                          "posts the chase publicly; keep group completers "
                          "for group-wide outcomes only")
            logger.warning("task %s: room delegated to origin group: %s",
                           task["id"], steer_note)
        target, note = await _resolve_completer(ctx, expected_completer)
        if target is None:
            # The model invented a plausible-looking key (live 2026-09-21:
            # 'session:Rupert Quekett') — steering would store a message no
            # dispatcher can ever claim. Keep the task; skip the delegation.
            steer_note = note
            logger.warning("task %s: %s", task["id"], steer_note)
        elif not await _steer_completer(ctx, task, target):
            steer_note = ("delegation stored but no dispatcher was available — "
                          "it stays undispatched for recovery")
    logger.info("task %s registered (waiter=%s completer=%s due=%s%s)",
                task["id"], waiter_session, expected_completer, due,
                f"; {steer_note}" if steer_note else "")
    task["steer_note"] = steer_note
    return task


async def _resolve_completer(ctx: AppContext, completer: str) -> tuple[str | None, str]:
    """Canonicalise a conversation-shaped completer key against the
    bindings/conversations tables. Returns (target_key, note): the target is
    the live conversation to steer (merge chains followed), or None with a
    caller-facing note when the key names nothing."""
    from server.repositories.conversations import ConversationRepository
    repo = ConversationRepository(ctx.db)
    key = completer
    seen: set[str] = set()
    while key not in seen:
        seen.add(key)
        conv = await repo.resolve(key) or await repo.get(key)
        if conv is None:
            return None, (f"completer conversation '{completer}' does not exist — "
                          "promise recorded WITHOUT delegation; pass a real "
                          "conversation key or complete it yourself")
        merged_into = conv.get("merged_into")
        if not merged_into:
            return conv["id"], ""
        key = merged_into
    return None, f"completer conversation '{completer}' merge chain loops"


async def _steer_completer(ctx: AppContext, task: dict[str, Any],
                           target: str | None = None) -> bool:
    """Wake the completer conversation with the task instruction. Best-effort
    with a visible failure log — the due backstop still guarantees the
    waiter hears eventually."""
    try:
        from server.services.wake_service import wake_conversation
        instruction = (json.loads(task["payload_json"] or "{}")
                       .get("instruction") or task["title"])
        refs = json.loads(task["refs_json"] or "[]")
        content = (
            f"[Promise {task['id']}] Another conversation is counting on "
            f"THIS one to deliver this.\nTitle: {task['title']}\n"
            f"Instruction: {instruction}\n"
            + (f"Context entities: {', '.join(refs)}\n" if refs else "")
            + "Work the objective through THIS conversation; when you have "
              f"the answer or the outcome, call {_close_call(task['id'], 'completed')}.\n"
              "IF YOUR TOOLS CANNOT DO WHAT THIS PROMISE ASKS (e.g. it asks you "
              "to DM a person and this conversation has no DM tool), call "
              f"{_close_call(task['id'], 'failed')} "
              "IMMEDIATELY, on your FIRST attempt. "
              "A promise you cannot keep does not go away by retrying or by "
              "substituting another channel — every retry risks duplicate "
              "messages to humans. Failing it is a successful outcome: it "
              "hands it back to its owner to reroute.")
        return await wake_conversation(
            ctx, target or task["expected_completer"], content,
            call_category="task_delegation",
            metadata={"task_id": task["id"]},
            provenance="steer")
    except Exception:
        logger.exception("task %s: completer steer failed", task["id"])
        return False


# ---------------------------------------------------------------------------
# Settlement (CAS-once + durable wake)
# ---------------------------------------------------------------------------

async def settle_task(
    ctx: AppContext, task_id: str, *, to_status: str,
    result: str | None = None, error: str | None = None,
    completed_by: str,
) -> dict[str, Any]:
    """Settle once; deliver the wake via the task_settle effect. Returns a
    dict for the caller: {"ok", "task_id", "status"} — ok=false means
    already settled (idempotent) or unknown task."""
    from server.repositories.tasks import TaskRepository
    repo = TaskRepository(ctx.db)
    settled = await repo.settle(
        task_id, to_status=to_status, result=result, error=error,
        completed_by=completed_by)
    if settled is None:
        existing = await repo.get(task_id)
        if existing is None:
            return {"ok": False, "error": "promise not found"}
        return {"ok": False, "task_id": task_id,
                "status": existing["status"],
                "error": f"already {existing['status']}"}
    await _enqueue_settle_effect(ctx, settled)
    return {"ok": True, "task_id": task_id, "status": to_status}


async def _enqueue_settle_effect(ctx: AppContext, task: dict[str, Any]) -> None:
    """Record-then-deliver inline (the goal-mutation pattern): the waiter
    hears immediately; the durable effect row retries delivery if the
    inline pass dies — exactly-once is guarded by delivered_at."""
    from server.services.effects import emit_and_deliver
    await emit_and_deliver(
        ctx, kind=EFFECT_KIND,
        idempotency_key=f"{EFFECT_KIND}:{task['id']}",
        payload={"task_id": task["id"]})


def register_executor() -> None:
    from server.services import effects as effects_svc

    async def _exec(ctx, payload):
        await deliver_settlement(ctx, payload["task_id"])
        return payload["task_id"]

    effects_svc.register_executor(EFFECT_KIND, _exec, retryable=True)


async def deliver_settlement(ctx: AppContext, task_id: str) -> bool:
    """The effect body: cancel the due backstop, wake the waiter with the
    provenance header + result (plan D12). Idempotent — replay-safe."""
    from server.repositories.tasks import TaskRepository
    from server.repositories.wakeups import WakeupRepository
    from server.services.wake_service import wake_conversation

    task = await TaskRepository(ctx.db).get(task_id)
    if task is None or task["status"] == "pending":
        return False
    if task.get("delivered_at"):
        return True  # already woken (effect replay)
    # Cancel the due backstop (payload match — the cancel_for_routine shape).
    await _cancel_task_due_wakeups(ctx, task_id)

    outcome = ("FAILED" if task["status"] == "failed"
               else task["status"].upper())
    body = task["result"] if task["status"] != "failed" else (
        f"{task['error'] or 'no reason recorded'}")
    content = (
        f"## Promise {outcome} — {task['title']}\n"
        f"Promise {task['id']} was {task['status']} by "
        f"{task.get('completed_by') or 'unknown'} at "
        f"{(task.get('completed_at') or '')[:19]} UTC.\n\n"
        f"{body or '(no result recorded)'}\n\n"
        "Fold this into whatever you were waiting on.")
    try:
        await wake_conversation(
            ctx, task["waiter_session"], content,
            call_category=f"task_{task['status']}",
            metadata={"task_id": task["id"],
                      "settled_by": task.get("completed_by")})
    except Exception:
        logger.exception("task %s: waiter wake failed", task_id)
        return False
    # Stamp AFTER a successful wake: a failed wake must stay undelivered so
    # the effect retries; the crash window the other way (woken, stamp
    # lost) costs at most one duplicate wake — benign against a lost one.
    await TaskRepository(ctx.db).mark_delivered(task_id)
    # Goal loop (docs/goal-execution-plan.md D4): the settled task IS
    # evidence — append it to the source goal's known-list so any later
    # round (or the dashboard) sees it without re-deriving from history.
    if task.get("source_goal_id"):
        try:
            from server.repositories.goals import GoalRepository
            excerpt = (task["result"] or task["error"] or "")[:400]
            await GoalRepository(ctx.db).append_known_line(
                task["source_goal_id"],
                f"[promise {task['id']} {task['status']}] "
                f"{task['title'][:120]}: {excerpt}")
        except Exception:
            logger.exception("task %s: goal evidence append failed", task_id)
    return True


async def _cancel_task_due_wakeups(ctx: AppContext, task_id: str) -> None:
    """Cancel the due backstop rows for a settled task. Wakeups SQL is
    wakeups-repo-owned; the payload-match cancel mirrors
    cancel_for_routine."""
    from server.repositories.wakeups import WakeupRepository
    repo = WakeupRepository(ctx.db)
    # repo-level seam: extend with a purpose-built method rather than raw SQL
    await repo.cancel_for_task(task_id)


# ---------------------------------------------------------------------------
# Completer-side visibility (plan D6)
# ---------------------------------------------------------------------------

async def tasks_block_lines(session_key: str, db) -> str:
    """The completer-side listing (instruction + pending promises), rendered
    under "Asked of this conversation" in the Work block — the reply hours
    later still sees the promise."""
    from server.repositories.tasks import TaskRepository
    pending = await TaskRepository(db).list_for_completer(session_key)
    if not pending:
        return ""
    lines = [
        "Another conversation is waiting on these. Work them through THIS "
        f"conversation; close each with {_close_call('<id>', 'completed')} — or "
        f"{_close_call('<id>', 'failed')} the MOMENT you see you cannot do it (no "
        "such tool in this conversation, no such contact, wrong channel). "
        "Retrying an undoable promise or substituting a group message for a DM "
        "request spams humans — fail it immediately and move on. Do "
        "not narrate a settlement you didn't perform.",
        "",
    ]
    for t in pending:
        instruction = (json.loads(t["payload_json"] or "{}")
                       .get("instruction") or "")
        lines.append(f"- {t['id']} (due {local_minute(t['due'])}): {t['title']}")
        if instruction:
            lines.append(f"    {instruction[:220]}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Check-in listing (rooms are first-class waiters — plan D11)
# ---------------------------------------------------------------------------

async def pending_tasks_lines(db, waiter_session: str) -> list[str]:
    from server.repositories.tasks import TaskRepository
    rows = await TaskRepository(db).list_for_waiter(waiter_session)
    return [
        f"- {t['id']} (due {local_minute(t['due'])}) [{t['expected_completer'] or 'open'}]"
        f" {t['title']}"
        for t in rows
    ]


# ---------------------------------------------------------------------------
# Reconciliation sweep (completer death ≠ task death)
# ---------------------------------------------------------------------------

async def reconcile_orphans(ctx: AppContext) -> dict[str, int]:
    """Boot/heartbeat sweep: fail pending tasks whose completer is dead
    (failed/gone subagent, dead bg unit, or 'script' past grace), so waiters
    hear 'completer died' instead of stalling to the due backstop."""
    from server.repositories.tasks import TaskRepository
    repo = TaskRepository(ctx.db)
    now = datetime.now(timezone.utc)
    failed = 0
    for t in await repo.pending_all():
        comp = t.get("expected_completer") or ""
        dead_reason = None
        if comp.startswith("subagent:") or _looks_like_subagent_id(comp):
            dead_reason = await _subagent_dead(ctx, comp)
        elif comp == "script":
            due = _parse(t["due"])
            if due and now > due + timedelta(
                    minutes=DEFAULT_DUE_MINUTES * (SCRIPT_GRACE_MULTIPLE - 1)):
                dead_reason = "script completer past grace (2× due)"
        elif comp.startswith("bg-"):
            dead_reason = await _unit_dead(comp)
        if dead_reason:
            res = await settle_task(
                ctx, t["id"], to_status="failed",
                error=f"completer died: {dead_reason} (reconcile sweep)",
                completed_by="system:sweep")
            if res.get("ok"):
                failed += 1
    if failed:
        logger.info("task reconcile: failed %d orphaned task(s)", failed)
    return {"failed": failed}


def _looks_like_subagent_id(ref: str) -> bool:
    import uuid
    try:
        uuid.UUID(ref)
        return True
    except ValueError:
        return False


async def _subagent_dead(ctx: AppContext, ref: str) -> str | None:
    from server.repositories.subagents import SubagentRepository
    row = await SubagentRepository(ctx.db).get(ref)
    if row is None:
        return "subagent row missing"
    if row["status"] in ("failed", "cancelled"):
        return f"subagent {row['status']}: {str(row.get('error_message') or '')[:120]}"
    return None


async def _unit_dead(unit_name: str) -> str | None:
    import subprocess
    try:
        out = subprocess.run(
            ["systemctl", "--user", "is-active", unit_name],
            capture_output=True, text=True, timeout=10)
        if out.stdout.strip() in ("inactive", "failed"):
            return f"bg unit {unit_name} {out.stdout.strip()}"
    except Exception:
        return None
    return None


def _parse(ts: str) -> datetime | None:
    try:
        dt = datetime.fromisoformat((ts or "").replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Tool surface (every dispatch path)
# ---------------------------------------------------------------------------

def make_task_tools(ctx: AppContext, session_key: str, *,
                    access: str = "full") -> list:
    """The work tool set attached wherever conversation turns run (chat,
    wakes, rooms): add_goal / close_goal / list_goals / delegate_goal /
    schedule_goal / accept_suggestion, gated by ``access`` (full | create |
    none — trusted / untrusted group / untrusted DM)."""
    from server.services.goal_work_tools import make_goal_work_tools
    return make_goal_work_tools(ctx, session_key, access=access)


def promise_tool_handlers(ctx: AppContext, session_key: str) -> list:
    """The promise handlers (register / complete / fail / cancel / list)
    that the unified work tools wrap — internal, never attached directly."""
    from server.services.tools import tool

    @tool
    async def task_register(
        title: str, instruction: str = "", completer_session: str = "",
        due_minutes: float = 0, refs: str = "[]",
    ) -> str:
        """Register a task THIS conversation waits on — including work you
        just promised to do ('I'll fix X', 'rebuilding it properly'):
        registering is how the promise survives the turn, because later
        turns only know what is registered. The completer (another
        conversation, a script, a subagent) settles it via task_complete /
        task_fail — or the script endpoint — and you are woken with the
        result. Pass completer_session to ALSO wake that conversation now
        with the instruction (delegation). due_minutes defaults to 60; long
        waits pass an explicit value. refs: JSON array of entity ids the
        completer's memory extraction should offer."""
        try:
            refs_list = json.loads(refs) if refs and refs != "[]" else []
            if not isinstance(refs_list, list):
                refs_list = []
        except json.JSONDecodeError:
            return json.dumps({"ok": False, "error": "refs must be a JSON array"})
        task = await register_task(
            ctx, waiter_session=session_key, title=title,
            instruction=instruction,
            expected_completer=completer_session.strip() or None,
            due_minutes=due_minutes or None, refs=refs_list)
        if not task.get("ok", True):
            return json.dumps(task)  # refusal: guidance, no task created
        resp = {"ok": True, "task_id": task["id"], "due": task["due"]}
        if task.get("steer_note"):
            resp["warning"] = task["steer_note"]
        return json.dumps(resp)

    @tool
    async def task_complete(task_id: str, result: str) -> str:
        """Settle a task as completed — you may be the completer named by
        another conversation, or closing out your own. task_id accepts the
        full id or the short suffix (e.g. a3f2). One settlement wins; yours
        is attributed to this conversation."""
        return json.dumps(await _resolve_and_settle(
            ctx, session_key, task_id, to_status="completed", result=result))

    @tool
    async def task_fail(task_id: str, error: str) -> str:
        """CALL THIS IMMEDIATELY when this conversation lacks what the task
        needs (a tool, a channel, a contact) — that is the task succeeding
        at rerouting, not you failing at it. Also for genuine failure: the
        reason rides to the waiter. Never retry an undoable task and never
        substitute a different channel — that spams humans."""
        return json.dumps(await _resolve_and_settle(
            ctx, session_key, task_id, to_status="failed", error=error))

    @tool
    async def task_cancel(task_id: str, reason: str = "") -> str:
        """Withdraw a task. The waiter's own conversation may always cancel;
        others should have a reason — it rides in the wake."""
        return json.dumps(await _resolve_and_settle(
            ctx, session_key, task_id, to_status="cancelled", result=reason))

    @tool
    async def list_tasks(status: str = "pending") -> str:
        """Tasks this conversation WAITS ON (and, if pending, tasks it is
        expected to COMPLETE). status: pending | completed | failed | cancelled."""
        from server.repositories.tasks import TaskRepository
        repo = TaskRepository(ctx.db)
        waiting = await repo.list_for_waiter(session_key, status=status)
        owing = (await repo.list_for_completer(session_key)
                 if status == "pending" else [])
        return json.dumps({"ok": True, "waiting_on": [
            {"task_id": t["id"], "title": t["title"], "due": t["due"],
             "completer": t.get("expected_completer")}
            for t in waiting], "expected_of_you": [
            {"task_id": t["id"], "title": t["title"], "due": t["due"]}
            for t in owing]})

    return [task_register, task_complete, task_fail, task_cancel, list_tasks]


async def _resolve_and_settle(
    ctx: AppContext, session_key: str, ref: str, *, to_status: str,
    result: str | None = None, error: str | None = None,
) -> dict[str, Any]:
    from server.repositories.tasks import TaskRepository
    repo = TaskRepository(ctx.db)
    task = await repo.get(ref.strip()) or await repo.get_by_short_id(ref)
    if task is None:
        return {"ok": False, "error": f"task {ref!r} not found"}
    return await settle_task(
        ctx, task["id"], to_status=to_status, result=result, error=error,
        completed_by=f"conversation:{session_key}")


register_executor()


async def repoint_task(ctx: AppContext, task_id: str, *,
                       waiter_session: str) -> bool:
    """Move a pending task (and its due backstop) to a new waiter — the
    safe carry-over for goal recreates. Both halves move together: the
    task row AND its already-scheduled task_due wakeup (2026-09-26: only
    the row moved, and the backstop woke the cancelled goal's room)."""
    from server.repositories.tasks import TaskRepository
    from server.repositories.wakeups import WakeupRepository
    n = await TaskRepository(ctx.db).repoint_waiter(task_id, waiter_session)
    if not n:
        return False
    await WakeupRepository(ctx.db).repoint_task_backstop(task_id, waiter_session)
    return True
