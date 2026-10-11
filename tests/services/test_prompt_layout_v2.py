"""Prompt layout v2 — the turn brief (docs/prompt-layout-plan.md Phase 3)."""

from server.services.prompt_assembler import (
    _take_block, _take_section, build_chat_messages,
)

SK = "agent:main:whatsapp:group:120363000000000001"
SYS = ("## Persona\nI am Bob.\n\n## Work\n### Goals this conversation holds\n- card game"
       "\n\n## Record discipline\nrecord things\n\n## Self-Brief\nHow I work:\n- x\n"
       "Since last night (new, not yet curated):\n- [practice] new\n- [limit] other\n\n"
       "## MCP sources\nmcp")


async def _build(db, monkeypatch, layout):
    monkeypatch.setenv("BOB_PROMPT_LAYOUT", layout)
    return await build_chat_messages(None, SK, db=db, system_content=SYS,
                                     claimed_ids=set(), current_model="gpt-6-luna",
                                     send_tool_name="send_whatsapp_message")


async def test_v1_default_unchanged(db, monkeypatch):
    m = await _build(db, monkeypatch, "")
    sysmsg = m[0]["content"]
    assert "## Before you finish — HARD RULES" in sysmsg and "## Work" in sysmsg
    assert not any(x.get("content", "").startswith("# Turn brief")
                   for x in m if isinstance(x.get("content"), str))


async def test_v2_moves_volatile_parts_into_a_final_brief(db, monkeypatch):
    m = await _build(db, monkeypatch, "v2")
    sysmsg, brief = m[0]["content"], m[-1]
    assert brief["role"] == "system" and brief["content"].startswith("# Turn brief")
    for moved in ("## Work", "Since last night", "Local time now", "HARD RULES",
                  "Model serving this turn"):
        assert moved in brief["content"] and moved not in sysmsg
    assert "## Record discipline" in sysmsg and "## Reply length" in sysmsg
    assert "How I work:" in sysmsg                     # curated core stays put


async def test_v2_only_on_whatsapp_dispatch_turns(db, monkeypatch):
    monkeypatch.setenv("BOB_PROMPT_LAYOUT", "v2")
    m = await build_chat_messages(None, SK, db=db, system_content=SYS)  # not dispatch
    assert "HARD RULES" in m[0]["content"]


def test_take_helpers():
    rest, sec = _take_section(SYS, "## Work")
    assert sec.startswith("## Work") and "card game" in sec and "## Work" not in rest
    assert "## Record discipline" in rest
    rest, blk = _take_block(SYS, "Since last night (new, not yet curated):")
    assert blk.count("\n- ") == 2 and "Since last night" not in rest
    assert _take_section("no section here", "## Work") == ("no section here", "")
