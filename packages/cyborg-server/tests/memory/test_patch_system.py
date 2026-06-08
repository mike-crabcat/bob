"""Tests for v7 claim-centric memory: entity creation, FTS rendering, template output."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from cyborg_server.services.memory.models import Claim, EntityDocument
from cyborg_server.services.memory.claim_types import render_entity


class TestTemplateRendering:
    def test_renders_person_claims(self):
        claims = [
            {"claim_type_key": "spouse", "object_id": "person-abc123", "value": None},
            {"claim_type_key": "food_preference", "object_id": None, "value": "loves Thai"},
            {"claim_type_key": "job", "object_id": None, "value": "Software Engineer"},
        ]
        result = render_entity("person", "Mike Cleaver", claims)
        assert "Mike Cleaver" in result
        assert "Spouse/Partner: person-abc123" in result
        assert "Food: loves Thai" in result
        assert "Job: Software Engineer" in result

    def test_renders_trip_claims(self):
        claims = [
            {"claim_type_key": "destination", "object_id": None, "value": "Bali, Indonesia"},
            {"claim_type_key": "start_date", "object_id": None, "value": "2026-08-01"},
            {"claim_type_key": "end_date", "object_id": None, "value": "2026-08-10"},
            {"claim_type_key": "member", "object_id": "person-7f3a91", "value": None},
        ]
        result = render_entity("trip", "Bali 2026", claims)
        assert "Bali 2026" in result
        assert "Members: person-7f3a91" in result
        assert "destination: Bali, Indonesia" in result

    def test_renders_multiple_values_as_list(self):
        claims = [
            {"claim_type_key": "interest", "object_id": None, "value": "surfing"},
            {"claim_type_key": "interest", "object_id": None, "value": "photography"},
        ]
        result = render_entity("person", "Alice", claims)
        assert "Interests:" in result
        assert "- surfing" in result
        assert "- photography" in result

    def test_empty_claims_returns_display_name_only(self):
        result = render_entity("person", "Unknown Person", [])
        assert result.strip() == "Unknown Person"


class TestEntityFTSUpdate:
    @pytest.mark.asyncio
    async def test_write_entity_renders_fts(self, ctx, tmp_path):
        from cyborg_server.services.memory.service import MemoryService
        from cyborg_server.services.memory.claim_service import write_claim

        svc = MemoryService(ctx)
        entity = EntityDocument(
            entity_id="person-mike-cleaver",
            entity_type="person",
            display_name="Mike Cleaver",
        )
        await svc.write_entity(tmp_path, entity)

        # Add a claim
        claim = Claim(
            id="claim-test-fts",
            claim_type_key="job",
            subject_id="person-mike-cleaver",
            value="Software Engineer",
            status="active",
            source_bulletins=["bulletin-test"],
            created_at=datetime.now(timezone.utc),
        )
        await write_claim(ctx.db, claim)

        # Re-render FTS
        await svc._update_entity_fts("person-mike-cleaver")

        # Verify FTS was updated
        row = await ctx.db.fetch_one(
            "SELECT rendered_body FROM memory_entities_fts WHERE entity_id = 'person-mike-cleaver'"
        )
        assert row is not None
        assert "Software Engineer" in row["rendered_body"]


class TestEnsureEntitiesForClaims:
    @pytest.mark.asyncio
    async def test_creates_entity_records_for_claim_subjects(self, ctx, tmp_path):
        from cyborg_server.services.memory.service import MemoryService

        svc = MemoryService(ctx)

        claims = [
            Claim(
                id="claim-test-1",
                claim_type_key="destination",
                subject_id="trip-bali-2026",
                value="Bali",
                status="active",
                source_bulletins=["bulletin-test"],
                created_at=datetime.now(timezone.utc),
            ),
        ]

        bulletin_row = await ctx.db.fetch_one(
            "SELECT * FROM memory_bulletins WHERE id = 'bulletin-test'"
        )
        from cyborg_server.services.memory.models import Bulletin
        bulletin = Bulletin(
            id="bulletin-test",
            created_at=datetime.now(timezone.utc),
            channel_id="test",
            source_type="test",
            source_id="test",
            content="Trip to Bali planned.",
        )

        entity_ids = await svc._ensure_entities_for_claims(claims, bulletin)

        assert "trip-bali-2026" in entity_ids
        row = await ctx.db.fetch_one(
            "SELECT * FROM memory_entities WHERE entity_id = 'trip-bali-2026'"
        )
        assert row is not None
        assert row["entity_type"] == "trip"
