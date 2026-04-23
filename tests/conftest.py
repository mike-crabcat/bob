"""Shared pytest fixtures for all test modules."""

from pathlib import Path
from uuid import uuid4

import pytest_asyncio
from cyborg_server.database import Database

SCHEMA_DIR = Path(__file__).parent.parent / "packages" / "cyborg-server" / "cyborg_server" / "schemas"


@pytest_asyncio.fixture
async def db():
    """Create a fresh in-memory test database for each test."""
    db_path = Path(f"/tmp/cyborg-test-{uuid4()}.db")

    db = Database(db_path=db_path, schema_dir=SCHEMA_DIR, pool_size=1)
    await db.connect()
    await db.apply_migrations()

    yield db

    await db.close()
    if db_path.exists():
        db_path.unlink()
