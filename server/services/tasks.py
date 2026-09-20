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

Kill switch: BOB_TASKS=off hides tools + injection; rows persist.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any

from server.context import AppContext
from server.services.base import BaseService, iso_utc

logger = logging.getLogger(__name__)

DEFAULT_DUE_MINUTES = 60          # Mike 2026-09-19 (Q3)
SCRIPT_GRACE_MULTIPLE = 2         # sweep: script tasks fail at 2× due
EFFECT_KIND = "task_settle"


def tasks_enabled() -> bool:
    return os.getenv("BOB_TASKS", "on").strip().lower() not in (
        "off", "0", "false", "no")


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
    if (expected_completer and expected_completer != waiter_session
            and ":" in expected_completer
            and not expected_completer.startswith("subagent")):
        await _steer_completer(ctx, task)
    logger.info("task %s registered (waiter=%s completer=%s due=%s)",
                task["id"], waiter_session, expected_completer, due)
    return task


async def _steer_completer(ctx: AppContext, task: dict[str, Any]) -> None:
    """Wake the completer conversation with the task instruction. Best-effort
    with a visible failure log — the due backstop still guarantees the
    waiter hears eventually."""
    try:
        from server.services.wake_service import wake_conversation
        instruction = (json.loads(task["payload_json"] or "{}")
                       .get("instruction") or task["title"])
        refs = json.loads(task["refs_json"] or "[]")
        content = (
            f"[Task {task['id']}] This conversation is asked to complete a "
            f"task for another conversation.\nTitle: {task['title']}\n"
            f"Instruction: {instruction}\n"
            + (f"Context entities: {', '.join(refs)}\n" if refs else "")
            + "Work the objective through THIS conversation; when you have "
              "the answer or the outcome, call task_complete with this task "
              "id. If it can't be done, task_fail with the reason.")
        await wake_conversation(
            ctx, task["expected_completer"], content,
            call_category="task_delegation",
            metadata={"task_id": task["id"]},
            provenance="steer")
    except Exception:
        logger.exception("task %s: completer steer failed", task["id"])


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
            return {"ok": False, "error": "task not found"}
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
        f"## Task {outcome} — {task['title']}\n"
        f"Task {task['id']} was {task['status']} by "
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

async def tasks_block(session_key: str, db) -> str:
    """'Tasks awaiting this conversation' — injected into every dispatch for
    the expected completer, the outreach-goal trick generalized: the reply
    hours later still sees the promise. Empty when none pending."""
    if not tasks_enabled():
        return ""
    from server.repositories.tasks import TaskRepository
    pending = await TaskRepository(db).list_for_completer(session_key)
    if not pending:
        return ""
    lines = [
        "## Tasks awaiting this conversation",
        "",
        "Another conversation is waiting on these. Work them through THIS "
        "conversation; settle each with task_complete(task_id, result) — or "
        "task_fail(task_id, error) if it can't be done. Do not narrate a "
        "settlement you didn't perform.",
        "",
    ]
    for t in pending:
        instruction = (json.loads(t["payload_json"] or "{}")
                       .get("instruction") or "")
        lines.append(f"- {t['id']} (due {t['due'][:16]}): {t['title']}")
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
        f"- {t['id']} (due {t['due'][:16]}) [{t['expected_completer'] or 'open'}]"
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

def make_task_tools(ctx: AppContext, session_key: str) -> list:
    """task_register / task_complete / task_fail / task_cancel / list_tasks —
    available wherever conversation turns run (chat, wakes, rooms)."""
    from server.services.tools import tool

    @tool
    async def task_register(
        title: str, instruction: str = "", completer_session: str = "",
        due_minutes: float = 0, refs: str = "[]",
    ) -> str:
        """Register a task THIS conversation waits on. The completer (another
        conversation, a script, a subagent) settles it via task_complete /
        task_fail — or the script endpoint — and you are woken with the
        result. Pass completer_session to ALSO wake that conversation now
        with the instruction (delegation). due_minutes defaults to 60; long
        waits pass an explicit value. refs: JSON array of entity ids the
        completer's memory extraction should offer."""
        if not tasks_enabled():
            return json.dumps({"ok": False, "error": "tasks disabled"})
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
        return json.dumps({"ok": True, "task_id": task["id"],
                           "due": task["due"]})

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
        """Settle a task as failed with the reason. Same rules as
        task_complete."""
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
