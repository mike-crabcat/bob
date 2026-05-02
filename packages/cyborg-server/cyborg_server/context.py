"""Shared application context passed to all services."""

from __future__ import annotations

from dataclasses import dataclass

from cyborg_server.config import Settings
from cyborg_server.database import Database


@dataclass
class AppContext:
    """Holds the shared runtime state available to every service."""

    db: Database
    settings: Settings
