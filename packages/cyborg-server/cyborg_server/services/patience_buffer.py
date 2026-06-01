"""Per-session buffers for the patience dispatch system."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any


@dataclass
class PendingItem:
    item_type: str  # "message" | "typing"
    timestamp: float  # monotonic clock
    sender_jid: str
    sender_name: str
    payload: dict[str, Any]


@dataclass
class PatienceBuffer:
    items: list[PendingItem] = field(default_factory=list)
    timer_handle: asyncio.TimerHandle | None = None
    last_activity: float = 0.0

    def add(self, item: PendingItem) -> None:
        self.items.append(item)
        self.last_activity = item.timestamp

    def cancel_timer(self) -> None:
        if self.timer_handle is not None:
            self.timer_handle.cancel()
            self.timer_handle = None

    def clear(self) -> None:
        self.cancel_timer()
        self.items.clear()
        self.last_activity = 0.0


class PatienceBufferRegistry:
    _buffers: dict[str, PatienceBuffer] = {}

    @classmethod
    def get(cls, session_key: str) -> PatienceBuffer:
        if session_key not in cls._buffers:
            cls._buffers[session_key] = PatienceBuffer()
        return cls._buffers[session_key]

    @classmethod
    def remove(cls, session_key: str) -> None:
        buf = cls._buffers.pop(session_key, None)
        if buf is not None:
            buf.cancel_timer()

    @classmethod
    def clear_all(cls) -> None:
        for buf in cls._buffers.values():
            buf.cancel_timer()
        cls._buffers.clear()
