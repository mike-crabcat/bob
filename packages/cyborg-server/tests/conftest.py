"""Shared test fixtures for cyborg-server."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from cyborg_server.config import Settings
from cyborg_server.context import AppContext
from cyborg_server.database import Database


SCHEMA_DIR = Path(__file__).resolve().parent.parent / "cyborg_server" / "schemas"


@pytest.fixture
async def db():
    """Provide a connected in-memory SQLite database with all migrations applied."""
    database = Database(
        db_path=Path(":memory:"),
        schema_dir=SCHEMA_DIR,
        pool_size=1,
    )
    await database.connect()
    await database.apply_migrations()
    yield database
    await database.close()


@pytest.fixture
async def ctx(db):
    """Provide an AppContext with the test database and default settings."""
    settings = Settings.from_env()
    return AppContext(db=db, settings=settings)
