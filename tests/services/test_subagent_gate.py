"""Subagent spawn gate (2026-10-06).

Subagents have no memory, chat history or contacts. 'local' is retired;
unknown agent types are refused instead of silently running as Claude;
Claude briefs that lean on Bob-only tools are refused with reroute advice.
"""

from __future__ import annotations

from server.services.subagent_service import SubagentService, spawn_refusal

DM = "agent:main:whatsapp:dm:61400000001"

BIOS_BRIEF = (
    "Write a D&D-style character bio for each member of the AI doom group "
    "(session_key: agent:main:whatsapp:group:f2915f33). Use "
    "get_session_messages to read a LOT of group history.")


def test_local_is_retired():
    r = spawn_refusal("summarise scratch/notes.md", "local")
    assert r and "retired" in r and "add_goal" in r


def test_unknown_type_refused_known_spellings_map():
    assert "unknown agent_type" in spawn_refusal("x", "price-checker")
    for ok in ("claude", "claude_code", "coder", "general", "research"):
        assert spawn_refusal("build the PDF from scratch/report.md", ok) is None, ok
    assert spawn_refusal("ask about stock", "phone-call") is None


def test_memory_briefs_refused_for_claude():
    assert "NOT SPAWNED" in spawn_refusal(BIOS_BRIEF, "claude")
    assert spawn_refusal("Read the group chat history and summarise it", "claude")
    assert spawn_refusal("call recall('David') first", "claude")


def test_plain_english_is_not_a_tool_name():
    # 'remember'/'recall' as words — real 2026-09 skill-building briefs
    assert spawn_refusal("Create a skill; remember to add a SKILL.md and "
                         "recall the template layout", "claude") is None


async def test_refused_spawn_writes_nothing(ctx):
    r = await SubagentService(ctx).create_subagent(BIOS_BRIEF, DM, agent_type="claude")
    assert not r["ok"] and "NOT SPAWNED" in r["error"]
    assert await ctx.db.fetch_one("SELECT id FROM subagents") is None
    assert await ctx.db.fetch_one("SELECT id FROM goals WHERE kind='promise'") is None


async def test_message_to_retired_local_refused(ctx):
    await ctx.db.execute(
        """INSERT INTO subagents (id, parent_session_key, session_key, task,
           status, agent_type, created_at, updated_at)
           VALUES ('loc-1', ?, 'subagent:x:1', 't', 'waiting_for_parent',
                   'local', '2026-10-01', '2026-10-01')""", (DM,))
    r = await SubagentService(ctx).message_subagent("loc-1", "more please")
    assert not r["ok"] and "retired" in r["error"]
