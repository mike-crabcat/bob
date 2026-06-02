"""Test that the heartbeat bulletin-generation path passes known_entities."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest


@pytest.mark.asyncio
async def test_generate_session_bulletin_passes_known_contacts(ctx, monkeypatch):
    """The bulletin generator must receive known_entities built from the contacts DB."""
    await ctx.db.execute(
        "INSERT INTO contacts (id, name, phone_number, email, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (
            "03f3902d-330b-4f15-bf2a-b1385a917677",
            "Blair Nicol",
            "+61401589328",
            "",
            datetime.now(timezone.utc).isoformat(),
            datetime.now(timezone.utc).isoformat(),
        ),
    )

    captured = {}

    async def fake_generate(llm, gen_input):
        captured["known_entities"] = gen_input.known_entities
        return (
            "---\n"
            "create_bulletin: false\n"
            "reason: test\n"
            "session_id: x\n"
            "---\n"
        )

    def fake_validate(text):
        return True, {"create_bulletin": False, "reason": "test", "session_id": "x"}

    import cyborg_server.services.memory.bulletin_generator as bg
    monkeypatch.setattr(bg, "generate_bulletin", fake_generate)
    monkeypatch.setattr(bg, "validate_draft_bulletin", fake_validate)

    session = {
        "session_key": "agent:main:whatsapp:dm:61401589328",
        "active_from": "2026-06-01T10:00:00",
        "last_message_at": "2026-06-01T10:05:00",
    }
    messages = [
        {"role": "user", "sender_id": "test", "content": "hello"},
        {"role": "assistant", "sender_id": "assistant", "content": "hi"},
    ]

    from cyborg_server import heartbeat

    await heartbeat._generate_session_bulletin(
        ctx, session, messages, {"test": "Blair"},
    )

    assert "contacts" in captured["known_entities"]
    ids = [c["id"] for c in captured["known_entities"]["contacts"]]
    assert "contact-03f3902d" in ids
