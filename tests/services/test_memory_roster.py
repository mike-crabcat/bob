"""Conversation memory roster (2026-09-13 rollout — memory-review follow-up).

The roster injects entity id + name + fact count for a group's participants
and recently-mentioned entities, gated per-conversation via the
`memory_roster` policy flag with a BOB_MEMORY_ROSTER kill switch.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from server.config import Settings
from server.context import AppContext
from server.database import Database
from server.services.context_assembler import ContextAssembler
from server.services.memory.service import build_conversation_roster

SCHEMA_DIR = Path(__file__).resolve().parent.parent.parent / "server" / "schemas"

GROUP_KEY = "agent:main:whatsapp:group:g123"
DM_KEY = "agent:main:whatsapp:dm:61400000001"
GROUP_JID = "120363000000000000@g.us"

CONTACT_UUID = "03f3902d-330b-4f15-bf2a-b1385a917677"


@pytest.fixture
async def db():
    database = Database(db_path=Path(":memory:"), schema_dir=SCHEMA_DIR, pool_size=1)
    await database.connect()
    await database.apply_migrations()
    yield database
    await database.close()


async def _seed_group_session(db, *, policy_flag: bool = False) -> None:
    now = "2026-09-13T00:00:00Z"
    await db.execute(
        "INSERT INTO conversations (id, kind, title, policy_json, created_at, updated_at) "
        "VALUES (?, 'group', 'Test Group', ?, ?, ?)",
        ("conv-g123", f'{{"memory_roster": {str(policy_flag).lower()}}}' if policy_flag else None, now, now))
    await db.execute(
        "INSERT INTO bindings (session_key, conversation_id, channel, kind, address, "
        "created_at, endpoint_kind, is_active) VALUES (?, ?, 'whatsapp', 'thread', ?, ?, 'group', 1)",
        (GROUP_KEY, "conv-g123", GROUP_JID, now))
    await db.execute(
        "INSERT INTO contacts (id, name, phone_number, created_at, updated_at) "
        "VALUES (?, 'Blair Nicol', '+61401589328', ?, ?)",
        (CONTACT_UUID, now, now))
    await db.execute(
        "INSERT INTO whatsappgroups (id, whatsapp_jid, name, member_count, created_at, updated_at) "
        "VALUES (?, ?, 'Test Group', 1, ?, ?)",
        ("grp1", GROUP_JID, now, now))
    await db.execute(
        "INSERT INTO whatsappgroup_members (id, group_id, contact_id, display_name, "
        "joined_at, created_at, updated_at) VALUES (?, ?, ?, 'Blair', ?, ?, ?)",
        ("gm1", "grp1", CONTACT_UUID, now, now, now))


async def _seed_memory(db) -> None:
    now = "2026-09-13T00:00:00Z"
    for eid, etype, name in [
        ("person-blair-nicol", "person", "Blair Nicol"),
        ("task-radio-poster", "task", "Radio Poster"),
    ]:
        await db.execute(
            "INSERT INTO memory_entities (entity_id, entity_type, display_name, status, created_at) "
            "VALUES (?, ?, ?, 'active', ?)",
            (eid, etype, name, now))
    # Blair's person entity is linked to his contact via the hex8 claim
    await db.execute(
        "INSERT INTO memory_claims (id, claim_type_key, subject_id, value, status, created_at) "
        "VALUES ('c1', 'contact_id', 'person-blair-nicol', ?, 'active', ?)",
        (CONTACT_UUID[:8], now))
    await db.execute(
        "INSERT INTO memory_claims (id, claim_type_key, subject_id, value, status, created_at) "
        "VALUES ('c2', 'music_preference', 'person-blair-nicol', 'jazz', 'active', ?)",
        (now,))
    await db.execute(
        "INSERT INTO memory_claims (id, claim_type_key, subject_id, value, status, created_at) "
        "VALUES ('c3', 'task_status', 'task-radio-poster', 'in-progress', 'active', ?)",
        (now,))
    await db.execute(
        "INSERT INTO memory_entity_mentions (entity_id, conversation_id, first_message_id, "
        "last_message_id, first_at, last_at) VALUES (?, ?, 'm1', 'm2', ?, ?)",
        ("task-radio-poster", "conv-g123", now, now))


def _assembler(db) -> ContextAssembler:
    ctx = AppContext(db=db, settings=Settings.from_env())
    return ContextAssembler(ctx)


async def test_memory_roster_lists_participants_then_mentions(db):
    await _seed_group_session(db)
    await _seed_memory(db)

    roster = await build_conversation_roster(db, GROUP_KEY)

    assert "## Memory Roster" in roster
    # participant-resolved entity first, with display name + fact count
    # (2 active claims on person-blair-nicol: contact_id + music_preference)
    blair_line = next(ln for ln in roster.splitlines() if "person-blair-nicol" in ln)
    assert "Blair Nicol" in blair_line
    assert "(2 facts)" in blair_line
    # mentioned entity included
    task_line = next(ln for ln in roster.splitlines() if "task-radio-poster" in ln)
    assert "(1 facts)" in task_line
    assert roster.index("person-blair-nicol") < roster.index("task-radio-poster")
    # guidance points at the tools, not the contents
    assert "get_entity" in roster


async def test_memory_roster_empty_when_nothing_known(db):
    await _seed_group_session(db)
    assert await build_conversation_roster(db, GROUP_KEY) == ""


async def test_maybe_memory_roster_off_without_policy(db):
    await _seed_group_session(db, policy_flag=False)
    await _seed_memory(db)
    assert await _assembler(db).maybe_memory_roster(GROUP_KEY) == ""


async def test_maybe_memory_roster_on_with_policy(db):
    await _seed_group_session(db, policy_flag=True)
    await _seed_memory(db)
    roster = await _assembler(db).maybe_memory_roster(GROUP_KEY)
    assert "person-blair-nicol" in roster


async def test_maybe_memory_roster_kill_switch(db, monkeypatch):
    await _seed_group_session(db, policy_flag=True)
    await _seed_memory(db)
    asm = _assembler(db)
    monkeypatch.setattr(asm.ctx.settings.memory, "roster_enabled", False)
    assert await asm.maybe_memory_roster(GROUP_KEY) == ""


async def test_maybe_memory_roster_ignores_dms(db):
    await _seed_group_session(db, policy_flag=True)
    await _seed_memory(db)
    assert await _assembler(db).maybe_memory_roster(DM_KEY) == ""
