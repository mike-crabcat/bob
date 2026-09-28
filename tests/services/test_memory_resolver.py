"""Resolver priority tests (2026-09-28 recall resolver bug).

recall("Marcus Bell") resolved to an incumbent whose body merely
contained a similar token — the embedding step outranked an entity
literally named the query, because display_name was never consulted.
Pins: display-name exact match beats fuzzy embedding similarity and
beats FTS token matching; aliases still win over display names.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from server.database import Database

SCHEMA_DIR = Path(__file__).parent.parent.parent / "server" / "schemas"


@pytest.fixture
async def db():
    database = Database(db_path=Path(":memory:"), schema_dir=SCHEMA_DIR,
                        pool_size=1)
    await database.connect()
    await database.apply_migrations()
    yield database
    await database.close()


async def _seed(db, entity_id, name, body):
    await db.execute(
        "INSERT INTO memory_entities (entity_id, entity_type, display_name, "
        "status) VALUES (?, 'person', ?, 'active')", (entity_id, name))
    await db.execute(
        "INSERT INTO memory_entities_fts (entity_id, display_name, "
        "rendered_body) VALUES (?, ?, ?)", (entity_id, name, body))


async def test_display_name_beats_embedding_decoy(db, monkeypatch):
    """The bug: embedding similarity returned a fuzzy incumbent while an
    entity named exactly the query existed. Display-name must outrank it."""
    from server.services.memory import tools as memory_tools

    await _seed(db, "person-marcus-bell", "Marcus Bell",
                "work_schedule: WFH Mondays and Fridays")
    await _seed(db, "person-ben", "Ben",
                "Referenced by: Marcel, Marcel, Marcel")  # the incumbent

    async def _decoy_search(db_, query, limit=5, threshold=1.2):
        return [{"entity_id": "person-ben", "distance": 0.5}]

    monkeypatch.setattr(
        "server.services.memory.embedding.search_similar", _decoy_search)

    resolved = await memory_tools._resolve_entity(db, "Marcus Bell")
    assert resolved is not None
    assert resolved["entity_id"] == "person-marcus-bell"


async def test_display_name_beats_fts_tokens(db):
    """Even with no embedding hit, an exact display name must not lose to
    FTS token matching (the pre-fix FTS fallback ANDs query tokens and
    can surface an unrelated incumbent first)."""
    from server.services.memory import tools as memory_tools

    await _seed(db, "person-marcus-bell", "Marcus Bell",
                "work_schedule: WFH Mondays and Fridays")
    await _seed(db, "person-other", "Someone Else",
                "Marcus Bell Marcus Bell lunch tradition")  # FTS bait

    resolved = await memory_tools._resolve_entity(db, "Marcus Bell")
    assert resolved["entity_id"] == "person-marcus-bell"


async def test_alias_still_wins(db):
    await _seed(db, "person-marcus-bell", "Marcus Bell", "claims: x")
    await _seed(db, "person-ben", "Ben", "claims: y")
    await db.execute(
        "INSERT INTO memory_aliases (alias, entity_id) VALUES "
        "('Marcus Bell', 'person-ben')")
    from server.services.memory import tools as memory_tools
    resolved = await memory_tools._resolve_entity(db, "Marcus Bell")
    assert resolved["entity_id"] == "person-ben"  # alias outranks display name
