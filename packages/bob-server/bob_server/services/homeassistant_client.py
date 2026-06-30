"""Thin HTTP client for the Home Assistant REST API.

Pull-based: Bob queries HA on demand via ``current_location()`` rather than
ingesting a continuous location stream. See services/location_tools.py.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import httpx

logger = logging.getLogger(__name__)


_CACHE_TTL_SECONDS: float = 120.0


class HomeAssistantClient:
    """Async HTTP client wrapping the Home Assistant REST API.

    Mirrors the AgentMailClient pattern (services/agentmail_client.py):
    httpx.AsyncClient with bearer auth, base_url, async close.

    Adds a small in-memory cache on ``get_state`` so that bursts of
    ``current_location()`` calls within ~2 minutes don't all hit HA.
    """

    def __init__(self, base_url: str, token: str, *, timeout: float = 10.0) -> None:
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            timeout=httpx.Timeout(timeout),
        )
        # entity_id -> (fetched_at_monotonic, payload)
        self._state_cache: dict[str, tuple[float, dict[str, Any]]] = {}

    async def get_state(self, entity_id: str, *, force_refresh: bool = False) -> dict[str, Any] | None:
        """GET /api/states/{entity_id}. Returns parsed JSON, or None on 404.

        Cached per entity_id for ``_CACHE_TTL_SECONDS``. Network errors are
        logged and re-raised — the tool layer is responsible for translating
        them into a user-facing message.
        """
        now = time.monotonic()
        if not force_refresh:
            cached = self._state_cache.get(entity_id)
            if cached and (now - cached[0]) < _CACHE_TTL_SECONDS:
                return cached[1]

        response = await self._client.get(f"/api/states/{entity_id}")
        if response.status_code == 404:
            return None
        response.raise_for_status()
        payload = response.json()
        self._state_cache[entity_id] = (now, payload)
        return payload

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "HomeAssistantClient":
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self.aclose()
