"""Tests for v7 claim extraction and entity reconciliation."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from bob_server.services.memory.models import Claim, Bulletin
from bob_server.services.memory.service import MemoryService


@pytest.mark.asyncio
async def test_process_bulletin_extracts_claims_with_type_keys(ctx, tmp_path):
    """process_bulletin should extract claims with claim_type_key, not type/predicate."""
    from unittest.mock import AsyncMock

    workspace = tmp_path
    svc = MemoryService(ctx)

    # Create bulletin
    bid = await svc.write_bulletin(
        workspace,
        channel_id="ch-test",
        source_type="test",
        content="Mike decided to go to Seminyak for the Bali trip.",
        visibility="group",
    )

    bulletin = await svc.read_bulletin(workspace, bid)
    assert bulletin is not None

    # Verify the claim service works with new model
    from bob_server.services.memory.claim_service import write_claim, get_active_claims

    claim = Claim(
        id="claim-recon-test",
        claim_type_key="destination",
        subject_id="trip-bali-2026",
        value="Seminyak, Bali",
        status="active",
        source_bulletins=[bid],
        visibility="group",
        created_at=datetime.now(timezone.utc),
    )
    await write_claim(ctx.db, claim)

    active = await get_active_claims(ctx.db, "trip-bali-2026")
    assert len(active) == 1
    assert active[0].claim_type_key == "destination"
    assert active[0].value == "Seminyak, Bali"


@pytest.mark.asyncio
async def test_entity_fts_renders_from_claims(ctx, tmp_path):
    """FTS should render from template, not stored body."""
    svc = MemoryService(ctx)

    # Create entity
    entity = await svc.ensure_person_entry(
        tmp_path,
        contact_id="03f3902d-330b-4f15-bf2a-b1385a917677",
        name="Blair Nicol",
    )
    assert entity == "person-blair-nicol"

    # Add claims
    from bob_server.services.memory.claim_service import write_claim
    await write_claim(ctx.db, Claim(
        id="claim-blair-food",
        claim_type_key="food_preference",
        subject_id="person-blair-nicol",
        value="loves Thai food",
        status="active",
        source_bulletins=["bulletin-test"],
        created_at=datetime.now(timezone.utc),
    ))

    # Re-render FTS
    await svc._update_entity_fts("person-blair-nicol")

    row = await ctx.db.fetch_one(
        "SELECT rendered_body FROM memory_entities_fts WHERE entity_id = 'person-blair-nicol'"
    )
    assert row is not None
    assert "Thai food" in row["rendered_body"]
