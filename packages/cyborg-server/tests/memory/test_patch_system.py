"""Tests for entity patch system: _apply_entity_patches, _derive_related_from_claims, dual-mode updates."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from cyborg_server.services.memory.models import ENTITY_CATEGORIES, Claim
from cyborg_server.services.memory.service import MemoryService


# ── _apply_entity_patches ────────────────────────────────────────────


class TestApplyEntityPatches:
    def test_patch_action_replaces_text(self):
        body = "## Summary\n\nLikes beer.\n"
        patches = [{"action": "patch", "search": "Likes beer.", "replace": "Likes beer and wine."}]
        result = MemoryService._apply_entity_patches(body, patches)
        assert "Likes beer and wine." in result
        assert result.count("Likes beer") == 1

    def test_patch_action_skips_when_search_not_found(self):
        body = "## Summary\n\nLikes beer.\n"
        patches = [{"action": "patch", "search": "Likes whiskey.", "replace": "Likes whiskey and soda."}]
        result = MemoryService._apply_entity_patches(body, patches)
        assert result == body

    def test_append_action_adds_to_section(self):
        body = "## Summary\n\nA person.\n\n## Timeline\n\n- 2026-01-01: Born.\n"
        patches = [{"action": "append", "section": "Timeline", "content": "- 2026-06-05: Got tested."}]
        result = MemoryService._apply_entity_patches(body, patches)
        assert "- 2026-06-05: Got tested." in result
        assert "- 2026-01-01: Born." in result

    def test_append_creates_section_if_missing(self):
        body = "## Summary\n\nBrief.\n"
        patches = [{"action": "append", "section": "Timeline", "content": "- 2026-06-05: Started."}]
        result = MemoryService._apply_entity_patches(body, patches)
        assert "## Timeline\n" in result
        assert "- 2026-06-05: Started." in result

    def test_create_action_returns_full_content(self):
        body = ""
        content = "## Summary\n\nNew entity.\n\n## Timeline\n\n- 2026-06-05: Created.\n"
        patches = [{"action": "create", "content": content}]
        result = MemoryService._apply_entity_patches(body, patches)
        assert result == content

    def test_multiple_patches_applied_sequentially(self):
        body = "## Summary\n\nLikes beer.\n\n## Timeline\n\n- 2026-01-01: Event.\n"
        patches = [
            {"action": "patch", "search": "Likes beer.", "replace": "Likes beer and wine."},
            {"action": "append", "section": "Timeline", "content": "- 2026-06-05: New event."},
        ]
        result = MemoryService._apply_entity_patches(body, patches)
        assert "Likes beer and wine." in result
        assert "- 2026-06-05: New event." in result

    def test_empty_patches_returns_body_unchanged(self):
        body = "## Summary\n\nContent.\n"
        result = MemoryService._apply_entity_patches(body, [])
        assert result == body


# ── _derive_related_from_claims ──────────────────────────────────────


class TestDeriveRelatedFromClaims:
    @pytest.mark.asyncio
    async def test_extracts_peer_entity_ids_from_claims(self, ctx):
        from cyborg_server.services.memory.service import MemoryService

        svc = MemoryService(ctx)
        # Insert entities so the lookup finds their types
        await ctx.db.execute(
            "INSERT INTO memory_entities (entity_id, entity_type, display_name, status, body) "
            "VALUES ('trip-bali', 'trip', 'Bali Trip', 'active', '')",
        )
        await ctx.db.execute(
            "INSERT INTO memory_entities (entity_id, entity_type, display_name, status, body) "
            "VALUES ('contact-abc', 'contact', 'Alice', 'active', '')",
        )

        claims = [
            Claim(
                id="c1", type="fact", subject_id="trip-bali",
                predicate="owner", object_id="contact-abc",
                status="active", source_bulletins=["b1"], visibility="group",
                scope=[], created_at=datetime.now(timezone.utc),
                superseded_by=[], body="trip-bali is owned by contact-abc.",
            ),
        ]

        related = await svc._derive_related_from_claims("trip-bali", claims)

        assert "contact-abc" in related.get("contacts", [])
        assert "trip-bali" not in related.get("trips", [])  # self excluded

    @pytest.mark.asyncio
    async def test_merges_with_existing_relations(self, ctx):
        from cyborg_server.services.memory.service import MemoryService

        svc = MemoryService(ctx)
        await ctx.db.execute(
            "INSERT INTO memory_entities (entity_id, entity_type, display_name, status, body) "
            "VALUES ('task-1', 'task', 'Task 1', 'active', '')",
        )

        existing = {"tasks": ["task-old"], "contacts": []}
        claims = [
            Claim(
                id="c1", type="task", subject_id="trip-bali",
                predicate="has_task", object_id="task-1",
                status="active", source_bulletins=["b1"], visibility="group",
                scope=[], created_at=datetime.now(timezone.utc),
                superseded_by=[], body="trip-bali has task-1.",
            ),
        ]

        related = await svc._derive_related_from_claims("trip-bali", claims, existing)

        assert "task-old" in related.get("tasks", [])
        assert "task-1" in related.get("tasks", [])

    @pytest.mark.asyncio
    async def test_empty_claims_returns_initialized_categories(self, ctx):
        from cyborg_server.services.memory.service import MemoryService

        svc = MemoryService(ctx)
        related = await svc._derive_related_from_claims("trip-bali", [])

        for cat in ENTITY_CATEGORIES:
            assert cat in related
            assert related[cat] == []

    @pytest.mark.asyncio
    async def test_unknown_entity_type_ignored(self, ctx):
        from cyborg_server.services.memory.service import MemoryService

        svc = MemoryService(ctx)
        # No entity in DB for this ID, so type lookup returns empty
        claims = [
            Claim(
                id="c1", type="fact", subject_id="trip-bali",
                predicate="x", object_id="unknown-thing",
                status="active", source_bulletins=[], visibility="group",
                scope=[], created_at=datetime.now(timezone.utc),
                superseded_by=[], body="x",
            ),
        ]

        related = await svc._derive_related_from_claims("trip-bali", claims)
        # unknown-thing has no entity row, so no category match
        for cat in ENTITY_CATEGORIES:
            assert "unknown-thing" not in related[cat]


# ── Dual-mode _update_single_entity ─────────────────────────────────


class TestDualModeEntityUpdate:
    @pytest.mark.asyncio
    async def test_patch_mode_uses_patch_prompt(self, ctx, tmp_path):
        svc = MemoryService(ctx)

        # Pre-create entity
        await ctx.db.execute(
            "INSERT INTO memory_entities (entity_id, entity_type, display_name, status, body) "
            "VALUES ('trip-bali', 'trip', 'Bali Trip', 'active', '## Summary\n\nA trip.\n')",
        )
        # Pre-create bulletin referenced by claim
        await ctx.db.execute(
            "INSERT INTO memory_bulletins (id, channel_id, source_type, content, created_at) "
            "VALUES ('b1', 'ch1', 'test', 'Mike decided Seminyak.', '2026-06-05')",
        )

        claims = [
            Claim(
                id="c1", type="decision", subject_id="trip-bali",
                predicate="accommodation_focus", object_id="location-seminyak",
                status="active", source_bulletins=["b1"], visibility="group",
                scope=[], created_at=datetime.now(timezone.utc),
                superseded_by=[], body="Focus on Seminyak.",
            ),
        ]

        async def fake_chat(messages, **kw):
            # Verify patch prompt is used
            system = messages[0]["content"]
            assert "search/replace" in system.lower() or "patch" in system.lower()
            return '[{"action":"patch","search":"A trip.","replace":"A trip to Bali, focusing on Seminyak."}]'

        llm = AsyncMock()
        llm.chat = fake_chat
        llm.memory_model = "test"

        result = await svc._update_single_entity(
            llm, "trip-bali", claims,
            all_existing_ids={"trip-bali"}, contact_name_map={},
            mode="patch",
        )

        assert result["count"] == 1
        row = await ctx.db.fetch_one("SELECT body FROM memory_entities WHERE entity_id = 'trip-bali'")
        assert "Seminyak" in row["body"]

    @pytest.mark.asyncio
    async def test_full_mode_uses_full_prompt(self, ctx, tmp_path):
        svc = MemoryService(ctx)

        await ctx.db.execute(
            "INSERT INTO memory_entities (entity_id, entity_type, display_name, status, body) "
            "VALUES ('trip-bali', 'trip', 'Bali Trip', 'active', '## Summary\n\nA trip.\n')",
        )
        await ctx.db.execute(
            "INSERT INTO memory_bulletins (id, channel_id, source_type, content, created_at) "
            "VALUES ('b1', 'ch1', 'test', 'Mike decided Seminyak.', '2026-06-05')",
        )

        claims = [
            Claim(
                id="c1", type="decision", subject_id="trip-bali",
                predicate="accommodation_focus", object_id="location-seminyak",
                status="active", source_bulletins=["b1"], visibility="group",
                scope=[], created_at=datetime.now(timezone.utc),
                superseded_by=[], body="Focus on Seminyak.",
            ),
        ]

        async def fake_chat(messages, **kw):
            system = messages[0]["content"]
            # Full prompt should NOT mention patch/search/replace
            assert "search/replace" not in system.lower()
            return (
                '[{"action":"write_entity","entity_id":"trip-bali","entity_type":"trip",'
                '"content":"---\\nentity_id: trip-bali\\nentity_type: trip\\n'
                'display_name: Bali Trip\\nstatus: active\\n---\\n\\n'
                '## Summary\\n\\nA trip to Bali, focusing on Seminyak.\\n"}]'
            )

        llm = AsyncMock()
        llm.chat = fake_chat
        llm.memory_model = "test"

        result = await svc._update_single_entity(
            llm, "trip-bali", claims,
            all_existing_ids={"trip-bali"}, contact_name_map={},
            mode="full",
        )

        assert result["count"] == 1
        row = await ctx.db.fetch_one("SELECT body FROM memory_entities WHERE entity_id = 'trip-bali'")
        assert "Seminyak" in row["body"]

    @pytest.mark.asyncio
    async def test_new_entity_uses_full_mode_regardless(self, ctx, tmp_path):
        svc = MemoryService(ctx)

        await ctx.db.execute(
            "INSERT INTO memory_bulletins (id, channel_id, source_type, content, created_at) "
            "VALUES ('b1', 'ch1', 'test', 'New trip decided.', '2026-06-05')",
        )

        claims = [
            Claim(
                id="c1", type="decision", subject_id="trip-phuket",
                predicate="created", object_id=None,
                status="active", source_bulletins=["b1"], visibility="group",
                scope=[], created_at=datetime.now(timezone.utc),
                superseded_by=[], body="Phuket trip created.",
            ),
        ]

        async def fake_chat(messages, **kw):
            # Even in patch mode, new entity should get the full prompt
            system = messages[0]["content"]
            assert "search/replace" not in system.lower()
            return (
                '[{"action":"create","entity_id":"trip-phuket","entity_type":"trip",'
                '"content":"---\\nentity_id: trip-phuket\\nentity_type: trip\\n'
                'display_name: Phuket Trip\\nstatus: active\\n---\\n\\n'
                '## Summary\\n\\nPhuket trip.\\n"}]'
            )

        llm = AsyncMock()
        llm.chat = fake_chat
        llm.memory_model = "test"

        result = await svc._update_single_entity(
            llm, "trip-phuket", claims,
            all_existing_ids=set(), contact_name_map={},
            mode="patch",  # requesting patch mode, but entity is new
        )

        assert result["count"] == 1
        row = await ctx.db.fetch_one("SELECT body FROM memory_entities WHERE entity_id = 'trip-phuket'")
        assert "Phuket" in row["body"]
