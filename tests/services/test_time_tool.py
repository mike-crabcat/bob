"""get_time tool + voice call-start clock (2026-09-01 time-grounding fan-out).

get_time lives in make_workspace_tools so it exists in every tool set the
system prompt's re-check sentence can reach (build_common_tools, generic
wake, local subagents). The voice builders carry a short no-tool stamp —
Realtime sessions have no bash, and outbound instructions are baked at
placement (prewarm), so the stamp is call-start, not answer-time.
"""

from __future__ import annotations

import re

from bob_server.services.workspace_tools import make_workspace_tools


def test_get_time_is_in_workspace_tools(ctx):
    tools = make_workspace_tools(ctx, session_key="agent:main:whatsapp:dm:61400000000")
    assert any(t.name == "get_time" for t in tools)


async def test_get_time_returns_stamp_and_iso(ctx):
    tools = make_workspace_tools(ctx)
    get_time = next(t for t in tools if t.name == "get_time")
    out = await get_time.handler()
    # same wall-clock shape as the system-prompt stamp, plus an ISO line
    assert re.search(
        r"[A-Z][a-z]+day \d{2} [A-Z][a-z]+ \d{4}, \d{2}:\d{2} "
        r"\([A-Za-z/_+-]+, UTC[+-]\d{2}:\d{2}\)", out)
    assert "(ISO: " in out


def test_outbound_instructions_carry_call_start_clock():
    from bob_server.services.voice_dispatch_service import build_outbound_instructions

    text = build_outbound_instructions("Alice", "Confirm the booking")
    assert "Current local time at call start:" in text
    assert "Trust this over any other sense of today's date." in text
    assert re.search(r"UTC[+-]\d{2}:\d{2}", text)


def test_inbound_instructions_carry_call_start_clock():
    from bob_server.services.voice_dispatch_service import build_inbound_instructions

    text = build_inbound_instructions("+61400000000", "Alice", "agenda text")
    assert "Current local time at call start:" in text
    assert "Trust this over any other sense of today's date." in text
    assert re.search(r"UTC[+-]\d{2}:\d{2}", text)
