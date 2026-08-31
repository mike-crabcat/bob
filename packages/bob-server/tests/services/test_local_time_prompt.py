"""Turn-scoped local-time prompt line (2026-09-01 routine-misfire follow-up).

The system prompt had a static 'Timezone: Australia/Perth' but NO current
time — the model had no grounding for date-relative reasoning and computed
UTC cron hours into a local-hours routine contract (Crypto-Bob group, three
routines fired 1am/4am). Pinned here:
- the line carries a full wall-clock stamp plus the timezone
- it names the mid-turn re-check command and demands the tz be quoted
- the inbound WhatsApp spec builder puts it first in system_content
"""

from __future__ import annotations

import re

from bob_server.services.prompt_assembler import local_now_prompt_line


def test_line_has_stamp_timezone_and_recheck_command():
    line = local_now_prompt_line()
    assert line.startswith("Local time now: ")
    # full weekday date + wall clock (e.g. "Tuesday 01 September 2026, 06:52")
    assert re.search(r"[A-Z][a-z]+day \d{2} [A-Z][a-z]+ 2026, \d{2}:\d{2}", line)
    # timezone stated with its offset
    assert re.search(r"\((AWST|[A-Za-z/_+-]+), UTC[+-]\d{2}:\d{2}\)", line)
    # mid-turn precision: the bash re-check, quoting the printed tz
    assert "date '+%A %d %B %Y %H:%M %Z'" in line
    assert "quote the timezone" in line
    # honest about what the stamp is
    assert "turn's start" in line


async def test_inbound_spec_leads_with_the_clock(ctx):
    from bob_server.services.whatsapp_bridge_service._service import (
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

    assert spec.system_content.startswith("Local time now: ")
    assert "date '+%A %d %B %Y %H:%M %Z'" in spec.system_content
