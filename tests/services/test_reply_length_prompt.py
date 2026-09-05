"""Reply-length guidance in WhatsApp turns (2026-09-05 verbosity A/B).

glm-5.3-flash blew past the persona's "Brevity is mandatory" rule on a
six-word group question (534-char confessional reply). The A/B harness
(real AI-doom prompt from the prod DB, 5 samples/variant) measured: a soft
rule at the system-message head plus a hard cap at the tail cuts mean
reply length 30-50% with voice retained; negative wording measured worse
than nothing. Pinned here:
- WhatsApp turns get the head rule first and the hard rule LAST in the
  system message (recency wins the adherence contest)
- group vs DM wording; non-WhatsApp channels untouched
- idempotent against callers that already carry a "## Reply length" block
"""

from __future__ import annotations

import pytest

from server.services.prompt_assembler import (
    _REPLY_LENGTH_HEAD, _REPLY_LENGTH_TAIL, build_chat_messages)


def _system(msgs):
    sys_msgs = [m for m in msgs if m["role"] == "system"]
    assert sys_msgs, "expected a system message"
    return sys_msgs[0]["content"]


@pytest.mark.asyncio
async def test_whatsapp_group_turn_gets_head_and_tail():
    msgs = await build_chat_messages(
        user_message="What model are you running Bob?",
        session_key="agent:main:whatsapp:group:120363060000000000",
        system_content="persona block")
    body = _system(msgs)
    assert body.startswith("## Reply length")
    assert "WhatsApp group chat" in body
    assert _REPLY_LENGTH_TAIL in body
    # tail is the LAST block — after the persona, at/after the clock slot
    assert body.rstrip().endswith(_REPLY_LENGTH_TAIL)


@pytest.mark.asyncio
async def test_whatsapp_dm_wording():
    msgs = await build_chat_messages(
        user_message="hey",
        session_key="agent:main:whatsapp:dm:61400000000",
        system_content="persona block")
    body = _system(msgs)
    assert "WhatsApp chat" in body
    assert "WhatsApp group chat" not in body


@pytest.mark.asyncio
async def test_non_whatsapp_channel_untouched():
    msgs = await build_chat_messages(
        user_message="hello",
        session_key="agent:main:email:thread-1",
        system_content="persona block")
    assert "## Reply length" not in _system(msgs)


@pytest.mark.asyncio
async def test_idempotent_when_caller_carries_block():
    msgs = await build_chat_messages(
        user_message="hello",
        session_key="agent:main:whatsapp:group:120363060000000000",
        system_content="## Reply length\nalready here")
    assert _system(msgs).count("## Reply length") == 1
    assert _REPLY_LENGTH_TAIL not in _system(msgs)
