"""Embedding service — OpenAI embeddings + sqlite-vec similarity search."""

from __future__ import annotations

import logging
import struct
from typing import Any

logger = logging.getLogger(__name__)

EMBEDDING_MODEL = "text-embedding-3-small"
EMBEDDING_DIMS = 1536


def _get_api_key() -> str:
    import os
    return os.getenv("CYBORG_OPENAI_API_KEY", "")


async def embed_text(text: str) -> list[float] | None:
    """Embed a single text string. Returns None on failure."""
    return (await embed_batch([text]))[0] if text.strip() else None


async def embed_batch(texts: list[str]) -> list[list[float] | None]:
    """Embed multiple texts via OpenAI. Returns list of vectors (or None per item on failure)."""
    api_key = _get_api_key()
    if not api_key:
        return [None] * len(texts)

    from openai import AsyncOpenAI
    client = AsyncOpenAI(api_key=api_key)

    # Filter out empty strings and track indices
    indexed: list[tuple[int, str]] = [(i, t) for i, t in enumerate(texts) if t.strip()]
    if not indexed:
        return [None] * len(texts)

    try:
        response = await client.embeddings.create(
            model=EMBEDDING_MODEL,
            input=[t for _, t in indexed],
        )
    except Exception:
        logger.warning("Embedding API call failed", exc_info=True)
        return [None] * len(texts)

    results: list[list[float] | None] = [None] * len(texts)
    for j, item in enumerate(response.data):
        orig_idx = indexed[j][0]
        results[orig_idx] = item.embedding

    return results


def _pack_embedding(vec: list[float]) -> bytes:
    """Pack a float vector into bytes for sqlite-vec storage."""
    return struct.pack(f"{len(vec)}f", *vec)


def _unpack_embedding(blob: bytes) -> list[float]:
    """Unpack bytes back to float vector."""
    count = len(blob) // 4
    return list(struct.unpack(f"{count}f", blob))


async def search_similar(
    db: Any,
    query: str,
    limit: int = 10,
    threshold: float = 1.2,
) -> list[dict[str, Any]]:
    """Embed the query and find similar entities via cosine distance.

    Returns list of {entity_id, distance} sorted by similarity.
    Only returns results with distance below threshold.
    """
    embedding = await embed_text(query)
    if embedding is None:
        return []

    packed = _pack_embedding(embedding)

    try:
        rows = await db.fetch_all(
            "SELECT entity_id, distance "
            "FROM memory_entity_embeddings "
            "WHERE embedding MATCH ? AND distance < ? "
            "ORDER BY distance "
            "LIMIT ?",
            (packed, threshold, limit),
        )
    except Exception:
        logger.warning("Embedding search query failed", exc_info=True)
        return []

    return [{"entity_id": r["entity_id"], "distance": r["distance"]} for r in rows]


async def upsert_embedding(db: Any, entity_id: str, embedding: list[float]) -> None:
    """Insert or replace an entity embedding."""
    packed = _pack_embedding(embedding)
    await db.execute(
        "INSERT OR REPLACE INTO memory_entity_embeddings(entity_id, embedding) VALUES (?, ?)",
        (entity_id, packed),
    )


async def delete_embedding(db: Any, entity_id: str) -> None:
    """Delete an entity embedding."""
    await db.execute(
        "DELETE FROM memory_entity_embeddings WHERE entity_id = ?",
        (entity_id,),
    )
