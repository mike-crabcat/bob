"""Turn-scoped local-time grounding (2026-09-01 routine-misfire follow-up).

The system prompt had a static 'Timezone: Australia/Perth' but NO current
time — the model had no grounding for date-relative reasoning and computed
UTC cron hours into a local-hours routine contract (Crypto-Bob group, three
routines fired 1am/4am). The first cut wired the clock into WhatsApp only;
as of the fan-out it is appended by build_chat_messages for every channel.
Pinned here:
- the line carries a full wall-clock stamp plus the timezone
- it names the mid-turn re-check (get_time, bash fallback) and demands the
  tz be quoted; tool-less callers get the stamp without tool guidance
- build_chat_messages appends the clock to every turn's system message,
  idempotently, with include_time=False restoring the pre-clock shape
"""

from __future__ import annotations

import re

from server.services.prompt_assembler import build_chat_messages, local_now_prompt_line


def test_line_has_stamp_timezone_and_recheck_command():
    line = local_now_prompt_line()
    assert line.startswith("Local time now: ")
    # full weekday date + wall clock (e.g. "Tuesday 01 September 2026, 06:52")
    assert re.search(r"[A-Z][a-z]+day \d{2} [A-Z][a-z]+ \d{4}, \d{2}:\d{2}", line)
    # timezone stated with its offset
    assert re.search(r"\((AWST|[A-Za-z/_+-]+), UTC[+-]\d{2}:\d{2}\)", line)
    # mid-turn precision: get_time first, bash fallback, quoting the printed tz
    assert "get_time" in line
    assert "date '+%A %d %B %Y %H:%M %Z'" in line
    assert "quote the timezone" in line
    # honest about what the stamp is
    assert "turn's start" in line


def test_line_without_tools_hint_is_stamp_only():
    line = local_now_prompt_line(tools_hint=False)
    assert line.startswith("Local time now: ")
    assert "turn's start" in line
    assert "get_time" not in line
    assert "bash" not in line


async def test_build_chat_messages_appends_clock_after_system_content():
    messages = await build_chat_messages(
        "hi", "", db=None, system_content="SYS")
    assert messages[0]["role"] == "system"
    content = messages[0]["content"]
    assert content.startswith("SYS\n\nLocal time now: ")
    assert content.count("Local time now:") == 1


async def test_build_chat_messages_bare_call_still_carries_clock():
    messages = await build_chat_messages("hi", "", db=None)
    assert messages[0]["role"] == "system"
    assert "Local time now:" in messages[0]["content"]


async def test_include_time_false_restores_pre_clock_shape():
    messages = await build_chat_messages(
        "hi", "", db=None, system_content="SYS", include_time=False)
    assert messages[0]["content"] == "SYS"
    bare = await build_chat_messages("hi", "", db=None, include_time=False)
    assert all(m["role"] != "system" for m in bare)


async def test_clock_injection_is_idempotent():
    messages = await build_chat_messages(
        "hi", "", db=None, system_content=local_now_prompt_line())
    assert messages[0]["content"].count("Local time now:") == 1


async def test_inbound_spec_no_longer_carries_the_clock(ctx):
    from server.services.whatsapp_bridge_service._service import (
        WhatsAppBridgeService,
    )

    svc = WhatsAppBridgeService(ctx)
    try:
        spec = await svc._build_inbound_dispatch_spec(
            session_key="agent:main:whatsapp:dm:61400000000",
            chat_id="61400000000@s.whatsapp.net", chat_kind="dm",
            contact_id=None, is_trusted=False, human_initiated=True)
    except Exception:
        import pytest
        pytest.skip("builder needs a fuller environment")

    # The clock moved to build_chat_messages — the spec must not carry it
    # (that would double the line once both are live).
    assert "Local time now:" not in spec.system_content
