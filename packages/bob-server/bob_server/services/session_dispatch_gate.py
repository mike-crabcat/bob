"""Per-session serialization lock for LLM dispatches."""

from __future__ import annotations

import asyncio


class SessionDispatchGate:
    """Ensures only one LLM dispatch runs per session at a time.

    Callers acquire the lock for their session_key before building
    context and running the LLM call.  This serialises concurrent
    messages to the same session and enables batching of queued
    messages.
    """

    _locks: dict[str, asyncio.Lock] = {}

    @classmethod
    def get_lock(cls, session_key: str) -> asyncio.Lock:
        if session_key not in cls._locks:
            cls._locks[session_key] = asyncio.Lock()
        return cls._locks[session_key]
