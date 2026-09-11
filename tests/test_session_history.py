"""SessionService.get_messages regression cover.

The messages table lost its session_key column in the 001_baseline squash;
_row_to_message read it from the row and raised KeyError on every call
(found live 2026-09-11 — get_contact_session_messages, i.e. Bob's
"review dm chat with <contact>" tool, had been failing 100% of the time).
"""

from __future__ import annotations

from server.services.session_service import SessionService


async def test_get_messages_returns_session_messages(ctx, db):
    session = "agent:main:whatsapp:dm:61400123456"
    await db.execute(
        "INSERT INTO messages (id, conversation_id, role, content, created_at) "
        "VALUES ('m1', ?, 'user', 'hi', '2026-09-11T10:00:00+00:00')",
        (session,),
    )
    await db.execute(
        "INSERT INTO messages (id, conversation_id, role, content, created_at) "
        "VALUES ('m2', ?, 'assistant', 'hello', '2026-09-11T10:00:05+00:00')",
        (session,),
    )

    msgs = await SessionService.from_db(db).get_messages(session, limit=10)

    assert [m.id for m in msgs] == ["m1", "m2"]
    assert all(m.session_key == session for m in msgs)
    assert [m.role for m in msgs] == ["user", "assistant"]
