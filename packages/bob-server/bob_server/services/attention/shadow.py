"""Shadow-mode decision recording (Bob3 Phase III item 6).

Computes what the Attention coordinator WOULD do for an accepted stimulus
and records it to attention_shadow. Audit-only: the live dispatcher's
behaviour is unchanged; any failure here is swallowed after logging.

Tier 1 windows (plan item 3): 10s DM / 20s group sliding window; addressed
messages get a 2–3s micro-window. Tier 2 (model probe) is NOT run in shadow
ingress — the shadow decision is ACT for addressed/DM stimuli and WAIT for
unaddressed group chatter (which Tier 2 would probe at window close).
"""

from __future__ import annotations

import logging
from typing import Any

from bob_server.services.attention.tier0 import detect_addressed

logger = logging.getLogger(__name__)

WINDOW_DM_MS = 10_000
WINDOW_GROUP_MS = 20_000
WINDOW_ADDRESSED_MS = 2_500


async def record_shadow_decision(
    db: Any,
    *,
    session_key: str,
    source: str,
    text: str = "",
    chat_kind: str = "dm",
    bot_name: str = "Bob",
    bot_jid: str | None = None,
    mentioned_jids: tuple[str, ...] | list[str] = (),
    reply_to_bot: bool = False,
    event_id: str | None = None,
) -> None:
    try:
        res = detect_addressed(
            text,
            bot_name=bot_name,
            chat_kind=chat_kind,
            bot_jid=bot_jid,
            mentioned_jids=mentioned_jids,
            reply_to_bot=reply_to_bot,
        )
        if res.addressed:
            window_ms = WINDOW_ADDRESSED_MS if chat_kind == "group" else WINDOW_DM_MS
            decision = "ACT"
        else:
            window_ms = WINDOW_GROUP_MS
            decision = "WAIT"
        await db.execute(
            """INSERT INTO attention_shadow
               (event_id, session_key, source, chat_kind, addressed,
                addressed_reason, proposed_window_ms, decision)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (event_id, session_key, source, chat_kind,
             1 if res.addressed else 0, res.reason, window_ms, decision),
        )
    except Exception:
        logger.warning("attention shadow recording failed for %s", session_key,
                       exc_info=True)
