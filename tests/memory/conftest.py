from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from bob_server.database import Database
from bob_server.context import AppContext
from bob_server.config import Settings
from bob_server.services.memory import MemoryService


SCHEMA_DIR = Path(__file__).resolve().parent.parent.parent / "bob_server" / "schemas"


@pytest.fixture
async def db():
    """In-memory DB with schema applied (no seed data)."""
    database = Database(db_path=Path(":memory:"), schema_dir=SCHEMA_DIR, pool_size=1)
    await database.connect()
    await database.apply_migrations()
    yield database
    await database.close()


@pytest.fixture
def workspace():
    return Path("/tmp/cyborg-test-workspace")


@pytest.fixture
async def svc(db):
    settings = Settings.from_env()
    ctx = AppContext(db=db, settings=settings)
    return MemoryService(ctx)


@pytest.fixture
async def memory_db():
    """In-memory DB with three contacts mirroring real production data."""
    db = Database(db_path=Path(":memory:"), schema_dir=SCHEMA_DIR, pool_size=1)
    await db.connect()
    await db.apply_migrations()

    now = datetime.now(timezone.utc).isoformat()
    rows = [
        ("03f3902d-330b-4f15-bf2a-b1385a917677", "Blair Nicol",  "+61401589328", ""),
        ("7c9f0fd7-6134-4495-aa8c-f04f11bc15e8", "Mike Cleaver", "+61456224867", "mike@crabcat.com"),
        ("b5d279cf-4c4d-4d6c-a7af-18efc507845d", "Helen Burnside","+61456224866", "burnside.helen@gmail.com"),
    ]
    for uuid, name, phone, email in rows:
        await db.execute(
            "INSERT INTO contacts (id, name, phone_number, email, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (uuid, name, phone, email, now, now),
        )
    yield db
    await db.close()
