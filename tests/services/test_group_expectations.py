"""Learned-expectation push (2026-09-13 AI doom lunch incident).

Group norms/traditions/open tasks render into group-turn prompts instead
of depending on the model choosing to recall (pull sat at 1.7% of group
turns). Reconciliation cannot sweep norm/correction claims. Plan links
dedupe.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from server.config import Settings
from server.context import AppContext
from server.database import Database
from server.services.context_assembler import ContextAssembler
from server.services.memory.reconciliation import (
    _recon_protection_reason,
    make_reconciliation_tools,
)
from server.services.memory.service import (
    build_conversation_roster,
    build_group_expectations,
)

SCHEMA_DIR = Path(__file__).resolve().parent.parent.parent / "server" / "schemas"

GROUP_KEY = "agent:main:whatsapp:group:g999"
GROUP_JID = "120363999999999999@g.us"
GROUP_EID = "group-testgroup"

NOW = "2026-09-13T00:00:00Z"


@pytest.fixture
async def db():
    database = Database(db_path=Path(":memory:"), schema_dir=SCHEMA_DIR, pool_size=1)
    await database.connect()
    await database.apply_migrations()
    yield database
    await database.close()


def _assembler(db) -> ContextAssembler:
    ctx = AppContext(db=db, settings=Settings.from_env())
    return ContextAssembler(ctx)


async def _seed_group(db, *, memory_entity_id: str | None = GROUP_EID) -> None:
    await db.execute(
        "INSERT INTO conversations (id, kind, title, policy_json, created_at, updated_at) "
        "VALUES ('conv-g999', 'group', 'Test Group', NULL, ?, ?)", (NOW, NOW))
    await db.execute(
        "INSERT INTO bindings (session_key, conversation_id, channel, kind, address, "
        "created_at, endpoint_kind, is_active) VALUES (?, ?, 'whatsapp', 'thread', ?, ?, 'group', 1)",
        (GROUP_KEY, "conv-g999", GROUP_JID, NOW))
    await db.execute(
        "INSERT INTO whatsappgroups (id, whatsapp_jid, name, member_count, "
        "memory_entity_id, created_at, updated_at) VALUES (?, ?, 'Test Group', 2, ?, ?, ?)",
        ("grp9", GROUP_JID, memory_entity_id, NOW, NOW))


async def _seed_claims(db) -> None:
    for eid, etype, name in [
        (GROUP_EID, "group", "Test Group"),
        ("task-plan-lunch", "task", "Plan Group Lunch"),
        ("task-done-thing", "task", "Finished Task"),
    ]:
        await db.execute(
            "INSERT INTO memory_entities (entity_id, entity_type, display_name, status, created_at) "
            "VALUES (?, ?, ?, 'active', ?)", (eid, etype, name, NOW))
    async def claim(cid, key, subject, value, at=NOW):
        await db.execute(
            "INSERT INTO memory_claims (id, claim_type_key, subject_id, value, status, created_at) "
            "VALUES (?, ?, ?, ?, 'active', ?)", (cid, key, subject, value, at))
    await claim("n1", "norm", GROUP_EID,
                "lunches are members-only — never apply the owner's family context")
    await claim("n2", "norm", GROUP_EID,
                "Bob proposes dates in-group; books only on majority + owner approval")
    await claim("t1", "tradition", GROUP_EID,
                "recurring group lunch; #1 Test Venue Tue 2026-09-01 (walk-in)")
    # open group task (related_entity → group) — no mention rows, old timestamp:
    # recency-only ordering would evict it, the pin must keep it
    await claim("s1", "task_status", "task-plan-lunch", "open", "2026-08-22T00:00:00Z")
    await db.execute(
        "INSERT INTO memory_claims (id, claim_type_key, subject_id, object_id, status, created_at) "
        "VALUES ('r1', 'related_entity', 'task-plan-lunch', ?, 'active', ?)", (GROUP_EID, "2026-08-22T00:00:00Z"))
    # a done task related to the group must NOT render
    await db.execute(
        "INSERT INTO memory_claims (id, claim_type_key, subject_id, object_id, status, created_at) "
        "VALUES ('r3', 'related_entity', 'task-done-thing', ?, 'active', ?)", (GROUP_EID, NOW))
    await db.execute(
        "INSERT INTO memory_claims (id, claim_type_key, subject_id, value, status, created_at) "
        "VALUES ('s3', 'task_status', 'task-done-thing', 'done', 'active', ?)", (NOW,))


async def test_group_expectations_renders_norms_traditions_open_tasks(db):
    await _seed_group(db)
    await _seed_claims(db)

    block = await build_group_expectations(db, GROUP_KEY)

    assert "How this group expects Bob to behave" in block
    assert "members-only" in block
    assert "Group traditions" in block
    assert "recurring group lunch" in block
    assert "Open tasks for this group" in block
    assert "task-plan-lunch" in block and "Plan Group Lunch" in block
    assert "task-done-thing" not in block  # done tasks don't render


async def test_group_expectations_empty_without_group_entity(db):
    await _seed_group(db, memory_entity_id=None)
    assert await build_group_expectations(db, GROUP_KEY) == ""


async def test_group_memory_hint_carries_pushed_expectations(db):
    await _seed_group(db)
    await _seed_claims(db)

    hint = await _assembler(db).group_memory_hint(GROUP_KEY)

    assert "## Group Memory" in hint
    assert f"recall('{GROUP_EID}')" in hint
    assert "How this group expects Bob to behave" in hint
    assert "task-plan-lunch" in hint


async def test_roster_pins_open_group_task_despite_no_mentions(db):
    await _seed_group(db)
    await _seed_claims(db)

    roster = await build_conversation_roster(db, GROUP_KEY)

    task_line = next(ln for ln in roster.splitlines() if "task-plan-lunch" in ln)
    assert "Plan Group Lunch" in task_line
    # done task not pinned
    assert "task-done-thing" not in roster


# ------------------------------------------------- reconciliation stickiness

def test_protection_reason_rules():
    assert _recon_protection_reason("c1", "norm", replacing=False) is not None
    assert _recon_protection_reason("c1", "norm", replacing=True) is not None
    assert _recon_protection_reason("claim-correct-abc", "truth", replacing=False) is not None
    assert _recon_protection_reason("claim-correct-abc", "truth", replacing=True) is not None
    # tradition: bare retract protected, supersede-with-update allowed
    assert _recon_protection_reason("c2", "tradition", replacing=False) is not None
    assert _recon_protection_reason("c2", "tradition", replacing=True) is None
    # ordinary claims unaffected
    assert _recon_protection_reason("claim-extr-abc", "preference", replacing=False) is None


async def test_retract_claim_refuses_norms_and_corrections(db):
    await _seed_group(db)
    await _seed_claims(db)
    await db.execute(
        "INSERT INTO memory_claims (id, claim_type_key, subject_id, value, status, created_at) "
        "VALUES ('claim-correct-test1', 'truth', ?, 'actually Tuesdays not Thursdays', 'active', ?)",
        (GROUP_EID, NOW))

    tools = {t.name: t for t in make_reconciliation_tools(db)}
    out_norm = await tools["retract_claim"].handler(
        subject_id=GROUP_EID, claim_type_key="norm")
    assert "SKIPPED" in out_norm
    row = await db.fetch_one("SELECT status FROM memory_claims WHERE id = 'n1'")
    assert row["status"] == "active"  # untouched

    out_corr = await tools["retract_claim"].handler(
        subject_id=GROUP_EID, claim_type_key="truth", old_value="actually Tuesdays not Thursdays")
    assert "SKIPPED" in out_corr or "Retracted 0" in out_corr
    row = await db.fetch_one("SELECT status FROM memory_claims WHERE id = 'claim-correct-test1'")
    assert row["status"] == "active"

    out_sup = await tools["supersede_claim_tool"].handler(
        subject_id=GROUP_EID, claim_type_key="norm",
        old_value="lunches are members-only — never apply the owner's family context",
        new_value="whatever")
    assert "SKIPPED" in out_sup
    row = await db.fetch_one("SELECT status FROM memory_claims WHERE id = 'n1'")
    assert row["status"] == "active"


# ------------------------------------------------------------- plan dedupe

async def test_session_plans_prompt_dedupes_link_rows(ctx):
    from server.services.dream.injection import build_session_plans_prompt

    now = "2026-09-13T00:00:00Z"
    sk = "agent:main:whatsapp:group:g999"
    # dream_plans.source_run_id has an FK → dream_runs
    await ctx.db.execute(
        "INSERT OR IGNORE INTO dream_runs (id, started_at, finished_at, window_start, window_end, status, trigger, model) "
        "VALUES ('dream-x', '2026-08-16T00:00:00Z', '2026-08-16T00:05:00Z', '2026-08-15T00:00:00Z', '2026-08-16T00:00:00Z', 'complete', 'cli', 'test')")
    await ctx.db.execute(
        """INSERT INTO dream_plans (id, title, what_was_discussed, proposed_action, assistance_method,
             status, evidence_json, source_run_id, created_at, updated_at)
           VALUES ('plan-dup1', 'Dentist lift', 'd', 'a', 'm', 'approved', ?, 'dream-x', ?, ?)""",
        (json.dumps([{"kind": "observed", "session_key": sk}]), now, now))
    # the same plan linked three times (observed 2026-09-13 in AI doom prompt)
    for _ in range(3):
        await ctx.db.execute(
            "INSERT INTO dream_item_links (item_type, item_id, session_key) VALUES ('plan', 'plan-dup1', ?)",
            (sk,))

    prompt = await build_session_plans_prompt(ctx.db, sk, dream_enabled=True)
    assert prompt.count("plan-dup1 [approved]") == 1
