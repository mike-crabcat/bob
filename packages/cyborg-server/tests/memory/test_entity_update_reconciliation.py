"""Tests for reconciliation in MemoryService._update_entities_from_claims."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from cyborg_server.services.memory.models import Claim
from cyborg_server.services.memory.service import MemoryService


@pytest.mark.asyncio
async def test_update_entities_reconciles_name_slug_to_canonical(ctx, tmp_path):
    """A claim with subject_id contact-blair-nicol + display_name Blair Nicol
    gets written to contact-03f3902d.md, not contact-blair-nicol.md."""
    workspace = tmp_path
    memory_dir = workspace / "memory"
    (memory_dir / "entities" / "contact").mkdir(parents=True)

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

    # Pre-existing display_name index for the duplicate id
    dup_path = memory_dir / "entities" / "contact" / "contact-blair-nicol.md"
    dup_path.write_text(
        "---\n"
        "entity_id: contact-blair-nicol\n"
        "entity_type: contact\n"
        "display_name: Blair Nicol\n"
        "status: active\n"
        "---\n\n"
        "# Blair Nicol\n",
        encoding="utf-8",
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
    wrote = await svc._update_entities_from_claims(llm, memory_dir, claims)

    assert wrote == 1
    # The canonical entity should exist
    assert (memory_dir / "entities" / "contact" / "contact-03f3902d.md").is_file()
    # The non-canonical one should NOT
    assert not (memory_dir / "entities" / "contact" / "contact-blair-nicol.md").is_file()
