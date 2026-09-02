"""Stale-subagent reap horizons (``SubagentRepository.fail_stale``).

The restart sweep alone can never catch rows whose owning task died
silently: voice calls that never reported an outcome (leaked 'running'
rows weeks old) and finished work whose parent session is long gone
('waiting_for_parent' forever). Age horizons reap both.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from bob_server.repositories.subagents import SubagentRepository

PARENT = "agent:main:whatsapp:group:120363000000000001"


def _iso_ago(**kwargs) -> str:
    return (datetime.now(timezone.utc) - timedelta(**kwargs)).isoformat()


async def _seed(db, subagent_id: str, *, status: str, agent_type: str,
                updated_at: str) -> None:
    await db.execute(
        """INSERT INTO subagents
           (id, parent_session_key, session_key, task, status, agent_type,
            created_at, updated_at)
           VALUES (?, ?, ?, 'do the thing', ?, ?, ?, ?)""",
        (subagent_id, PARENT, f"subagent:{subagent_id}", status, agent_type,
         updated_at, updated_at))


async def _status(db, subagent_id: str) -> str:
    row = await db.fetch_one(
        "SELECT status FROM subagents WHERE id = ?", (subagent_id,))
    return row["status"]


async def test_fail_stale_reaps_by_age_horizons(db):
    repo = SubagentRepository(db)
    OLD = _iso_ago(days=30)
    FRESH = _iso_ago(minutes=5)

    await _seed(db, "voice-old", status="running", agent_type="openai_voice",
                updated_at=OLD)      # leaked call — reaped
    await _seed(db, "voice-fresh", status="running", agent_type="openai_voice",
                updated_at=FRESH)    # phone still ringing — kept
    await _seed(db, "wfp-old", status="waiting_for_parent", agent_type="claude",
                updated_at=OLD)      # parent long gone — bookkeeping completed
    await _seed(db, "wfp-fresh", status="waiting_for_parent", agent_type="script",
                updated_at=FRESH)    # today's image script — kept
    await _seed(db, "coder-running", status="running", agent_type="coder",
                updated_at=FRESH)    # non-voice — restart sweep fails it

    count = await repo.fail_stale("2026-08-26T12:41:00+00:00")

    assert count == 3
    assert await _status(db, "voice-old") == "failed"
    assert await _status(db, "voice-fresh") == "running"
    assert await _status(db, "wfp-old") == "completed"
    assert await _status(db, "wfp-fresh") == "waiting_for_parent"
    assert await _status(db, "coder-running") == "failed"
    row = await db.fetch_one("SELECT result FROM subagents WHERE id = 'wfp-old'")
    assert row["result"] is None  # completion must not manufacture a result


async def test_fail_stale_completed_result_preserved(db):
    """Reaping a waiting_for_parent row keeps any stored result text."""
    repo = SubagentRepository(db)
    await db.execute(
        """INSERT INTO subagents
           (id, parent_session_key, session_key, task, status, agent_type,
            result, created_at, updated_at)
           VALUES ('wfp-result', ?, 'subagent:wfp-result', 'do the thing',
                   'waiting_for_parent', 'claude', 'the answer', ?, ?)""",
        (PARENT, _iso_ago(days=90), _iso_ago(days=80)))

    await repo.fail_stale("2026-08-26T12:41:00+00:00")

    row = await db.fetch_one(
        "SELECT status, result FROM subagents WHERE id = 'wfp-result'")
    assert row["status"] == "completed"
    assert row["result"] == "the answer"
