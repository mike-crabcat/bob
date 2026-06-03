"""Tests for reconciliation in MemoryService._update_entities_from_claims."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from cyborg_server.services.memory.models import Claim
from cyborg_server.services.memory.service import MemoryService


@pytest.mark.asyncio
async def test_update_entities_reconciles_name_slug_to_canonical(ctx, tmp_path):
    """A claim with subject_id contact-blair-nicol + display_name Blair Nicol
    gets written to contact-03f3902d in the DB, not contact-blair-nicol."""
    workspace = tmp_path

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

    # Pre-existing duplicate entity in DB
    await ctx.db.execute(
        "INSERT INTO memory_entities (entity_id, entity_type, display_name, status, body) "
        "VALUES ('contact-blair-nicol', 'contact', 'Blair Nicol', 'active', '# Blair Nicol\n')",
    )

    claims = [
        Claim(
            id="claim-test-001",
            type="fact",
            subject_id="contact-blair-nicol",
            predicate="likes_beer",
            object_id=None,
            status="active",
            source_bulletins=["bulletin-test"],
            visibility="private",
            scope=[],
            created_at=datetime.now(timezone.utc),
            superseded_by=[],
            body="Blair Nicol likes beer.",
        ),
    ]

    async def fake_chat(messages, **kw):
        return (
            '[{"action":"write_entity",'
            '"entity_id":"contact-blair-nicol",'
            '"entity_type":"contact",'
            '"content":"---\\n'
            'entity_id: contact-blair-nicol\\n'
            'entity_type: contact\\n'
            'display_name: Blair Nicol\\n'
            'status: active\\n'
            '---\\n\\n'
            '# Blair Nicol\\n\\n'
            '## Summary\\n\\nLikes beer.\\n"}]'
        )

    llm = AsyncMock()
    llm.chat = fake_chat
    llm.memory_model = "test"

    svc = MemoryService(ctx)
    wrote = await svc._update_entities_from_claims(llm, claims)

    assert wrote == 1
    # The canonical entity should exist in DB (created by reconciliation)
    canon = await ctx.db.fetch_one(
        "SELECT * FROM memory_entities WHERE entity_id = 'contact-03f3902d'"
    )
    assert canon is not None
    assert "Likes beer" in canon["body"]
