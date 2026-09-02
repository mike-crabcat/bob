"""Runtime dream configuration.

Autoplan is CONVERSATION-SCOPED: the flag lives in conversations.policy_json
(same idiom as /patience and /relevance), settable from the chat itself via
/autoplan, the CLI, or the dashboard without a restart. The env setting
(BOB_DREAM_AUTO_APPROVE_PLANS) is only the boot default for conversations
with no explicit flag. Route metadata is a one-deploy read fallback for rows
the 452 backfill missed.
"""

from __future__ import annotations

import logging

from server.database import Database
from server.services.base import iso_utc

logger = logging.getLogger(__name__)

ROUTE_META_KEY = "dream_autoplan"


async def get_session_autoplan(db: Database, session_key: str, boot_default: bool = False) -> bool:
    """Auto-approve state for one conversation; falls back to the boot default."""
    from server.repositories.conversations import ConversationRepository

    policy = await ConversationRepository(db).get_policy(session_key)
    flag = policy.get(ROUTE_META_KEY)
    if flag is not None:
        return bool(flag)
    return boot_default


async def set_session_autoplan(db: Database, session_key: str, enabled: bool) -> bool:
    """Set the per-conversation flag. Returns False for unknown conversations."""
    from server.repositories.conversations import ConversationRepository

    return await ConversationRepository(db).set_policy(
        session_key, {ROUTE_META_KEY: bool(enabled)})


async def list_autoplan_sessions(db: Database, *, enabled: bool = True) -> list[dict]:
    """Conversations with an explicit autoplan flag, for status displays."""
    rows = await db.fetch_all(
        "SELECT id FROM conversations "
        "WHERE json_extract(policy_json, '$.' || ?) = ?",
        (ROUTE_META_KEY, 1 if enabled else 0),
    )
    return [{"session_key": r["id"], "autoplan": enabled} for r in rows or []]
