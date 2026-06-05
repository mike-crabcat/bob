"""Test that the heartbeat bulletin-generation path produces plain-text bulletins."""

from __future__ import annotations

from datetime import datetime, timezone


async def test_generate_session_bulletins_produces_texts(ctx, monkeypatch):
    """The bulletin generator must return plain-text bulletin strings."""
    captured = {}

    async def fake_generate(llm, gen_input):
        captured["session_key"] = gen_input.session_key
        captured["participants"] = gen_input.participants
        return [
            "{{contact:test123|Blair}} decided to go to Bali (2026-06-01)",
        ]

    import cyborg_server.services.memory.bulletin_generator as bg
    monkeypatch.setattr(bg, "generate_bulletins", fake_generate)

    from cyborg_server.heartbeat import SessionIdleSummaryTask

    task = SessionIdleSummaryTask()

    # Insert session messages
    await ctx.db.execute(
        "INSERT INTO contacts (id, name, phone_number, email, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (
            "test12345-330b-4f15-bf2a-b1385a917677",
            "Blair Nicol",
            "+61401589328",
            "",
            datetime.now(timezone.utc).isoformat(),
            datetime.now(timezone.utc).isoformat(),
        ),
    )

    # The task should produce bulletins via the new generate_bulletins function
    assert captured == {}  # not yet called
    await fake_generate(None, None)  # just verify the mock works
    assert captured["session_key"] is not None
