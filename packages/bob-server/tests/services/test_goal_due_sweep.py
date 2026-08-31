"""Due-action scheduler — next_action dues become real triggers.

Pinned here (the 2026-08-31 AI doom coffee gap: the reminder action sat in
goal state with ``due: before 10am`` and nothing in the system read it):
- due extraction: ISO with offset, prose-padded ISO ("Before …"), naive →
  UTC, prose/garbage → None (no false triggers)
- the sweep: in-window dues get ONE action_due wakeup on the working
  conversation; idempotent across scheduled AND fired rows; window edges
  (>lookahead future, >overdue-hours past) are left alone
- delivery: pump_due_wakeups renders the action + due in the wake content
- claim_due compares parsed instants, not strings — a +08:00-offset
  not_before must fire at its true instant (goal-deadline wakeups carry
  whatever offset the deadline was written with)
- the reviser prompt demands ISO dues for time-bound goals
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

from bob_server.repositories.wakeups import WakeupRepository
from bob_server.services import goal_service
from bob_server.services.goal_service import extract_due_instant
from bob_server.services.goal_state_service import _reviser_system_prompt

WORK_KEY = "agent:main:whatsapp:group:120363422982048691"


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


async def _goal_with_due(ctx, due: str, *, objective: str = "organise the coffee") -> str:
    goal = await goal_service.create_goal(
        ctx, conversation_id=WORK_KEY, objective=objective, kind="coordination",
        strategy={"v": 2, "plan": "hold the coffee",
                  "known": [], "open_questions": [],
                  "next_actions": [{"action": "send the final reminder",
                                    "due": due}],
                  "refs": {"entities": [], "claims": []}})
    return goal["id"]


# ------------------------------------------------------------ extraction

def test_extract_due_instant_variants():
    now = datetime.now(timezone.utc)
    # Prose-padded ISO with offset — the live coffee-goal shape.
    padded = extract_due_instant(f"Before {now.isoformat()}")
    assert padded is not None and abs((padded - now).total_seconds()) < 1
    # Z suffix and bare +hhmm offset
    assert extract_due_instant("2026-09-01T02:00:00Z") is not None
    assert extract_due_instant("2026-09-01T02:00:00+0800") is not None
    # Naive → read as UTC
    naive = extract_due_instant("2026-09-01T02:00:00")
    assert naive == datetime(2026, 9, 1, 2, 0, tzinfo=timezone.utc)
    # No timestamp → never a trigger
    assert extract_due_instant("before the meetup") is None
    assert extract_due_instant("") is None
    assert extract_due_instant(None) is None


# ---------------------------------------------------------------- sweep

async def test_sweep_schedules_one_wake_for_in_window_due(ctx, db):
    due = datetime.now(timezone.utc) + timedelta(hours=6)
    goal_id = await _goal_with_due(ctx, f"Before {_iso(due)}")

    scheduled = await goal_service.schedule_due_action_wakes(ctx)
    assert scheduled == 1

    rows = await db.fetch_all(
        "SELECT * FROM wakeups WHERE goal_id = ? AND kind = 'action_due'",
        (goal_id,))
    assert len(rows) == 1
    assert rows[0]["conversation_id"] == WORK_KEY  # working conversation
    assert rows[0]["status"] == "scheduled"
    # Fires as the due entered the 12h window — i.e. now, not at due time.
    fire_at = datetime.fromisoformat(rows[0]["not_before"])
    assert fire_at - datetime.now(timezone.utc) < timedelta(minutes=1)


async def test_sweep_is_idempotent_across_fired_rows(ctx, db):
    due = datetime.now(timezone.utc) + timedelta(hours=6)
    goal_id = await _goal_with_due(ctx, _iso(due))

    assert await goal_service.schedule_due_action_wakes(ctx) == 1
    assert await goal_service.schedule_due_action_wakes(ctx) == 0
    # A fired wake must also count, or every later sweep would re-create it.
    await db.execute(
        "UPDATE wakeups SET status = 'fired' WHERE goal_id = ? AND kind = 'action_due'",
        (goal_id,))
    assert await goal_service.schedule_due_action_wakes(ctx) == 0


async def test_sweep_window_edges(ctx, db):
    now = datetime.now(timezone.utc)
    far_future = await _goal_with_due(
        ctx, _iso(now + timedelta(hours=30)), objective="too far out")
    far_past = await _goal_with_due(
        ctx, _iso(now - timedelta(hours=30)), objective="stale beyond overdue")
    recently_overdue = await _goal_with_due(
        ctx, _iso(now - timedelta(hours=2)), objective="just missed it")

    await goal_service.schedule_due_action_wakes(ctx)

    for goal_id in (far_future, far_past):
        rows = await db.fetch_all(
            "SELECT * FROM wakeups WHERE goal_id = ? AND kind = 'action_due'",
            (goal_id,))
        assert rows == [], "outside (overdue, lookahead] window — no wake"
    rows = await db.fetch_all(
        "SELECT * FROM wakeups WHERE goal_id = ? AND kind = 'action_due'",
        (recently_overdue,))
    assert len(rows) == 1, "recently-overdue dues are still chased once"


# ------------------------------------------------------------ delivery

async def test_pump_renders_action_due_wake(ctx, db, monkeypatch):
    wake = AsyncMock()
    monkeypatch.setattr(
        "bob_server.services.wake_service.wake_conversation", wake)

    due = datetime.now(timezone.utc) - timedelta(hours=1)
    await _goal_with_due(ctx, _iso(due))
    assert await goal_service.schedule_due_action_wakes(ctx) == 1

    fired = await goal_service.pump_due_wakeups(ctx)
    assert fired == 1
    wake.assert_awaited_once()
    args = wake.await_args
    assert args.args[1] == WORK_KEY
    assert "## Goal action due" in args.args[2]
    assert "send the final reminder" in args.args[2]
    assert _iso(due) in args.args[2]
    assert args.kwargs.get("call_category") == "goal_action_due"


# ------------------------------------------------- claim_due instants

async def test_claim_due_compares_instants_not_strings(db):
    """Goal-deadline wakeups carry the deadline's own offset (+08:00 live);
    string-comparing that against the UTC now fires ~8h late (or early)."""
    perth = timezone(timedelta(hours=8))
    repo = WakeupRepository(db)
    # 1h in the future, expressed in +08:00 — must NOT be due.
    await repo.schedule(
        conversation_id="c", goal_id=None,
        not_before=(datetime.now(timezone.utc) + timedelta(hours=1))
        .astimezone(perth).isoformat())
    assert await repo.claim_due() == []

    # 1 minute in the past, expressed in +08:00 — IS due.
    await repo.schedule(
        conversation_id="c", goal_id=None,
        not_before=(datetime.now(timezone.utc) - timedelta(minutes=1))
        .astimezone(perth).isoformat())
    claimed = await repo.claim_due()
    assert len(claimed) == 1


# ------------------------------------------------- reviser contract

def test_reviser_prompt_demands_iso_dues():
    prompt = _reviser_system_prompt()
    assert "ISO `due`" in prompt
    assert "prose dues never fire" in prompt
