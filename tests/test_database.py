"""Tests for Database connection pool and migrations."""

from __future__ import annotations

from pathlib import Path

import pytest

from bob_server.database import Database


SCHEMA_DIR = Path(__file__).resolve().parent.parent / "bob_server" / "schemas"


@pytest.fixture
async def db():
    database = Database(
        db_path=Path(":memory:"),
        schema_dir=SCHEMA_DIR,
        pool_size=1,
    )
    await database.connect()
    await database.apply_migrations()
    yield database
    await database.close()


async def test_health_check(db):
    assert await db.health_check() is True


async def test_fetch_one(db):
    row = await db.fetch_one("SELECT 42 AS value")
    assert row["value"] == 42


async def test_fetch_one_no_results(db):
    row = await db.fetch_one("SELECT 1 WHERE 0")
    assert row is None


async def test_fetch_all(db):
    rows = await db.fetch_all("SELECT 1 AS v UNION ALL SELECT 2")
    assert len(rows) == 2
    assert rows[0]["v"] == 1
    assert rows[1]["v"] == 2


async def test_execute_write(db):
    async with db.connection(write=True) as conn:
        await conn.execute("CREATE TABLE test_write (id INTEGER PRIMARY KEY)")
        await conn.execute("INSERT INTO test_write (id) VALUES (?)", (1,))
    row = await db.fetch_one("SELECT id FROM test_write")
    assert row["id"] == 1


async def test_execute_many(db):
    await db.execute("CREATE TABLE test_many (id INTEGER PRIMARY KEY)")
    await db.execute_many(
        "INSERT INTO test_many (id) VALUES (?)",
        [(1,), (2,), (3,)],
    )
    rows = await db.fetch_all("SELECT id FROM test_many ORDER BY id")
    assert len(rows) == 3


async def test_migrations_applied_once(db):
    """Running migrations again should be a no-op."""
    await db.apply_migrations()
    row = await db.fetch_one("SELECT COUNT(*) AS cnt FROM schema_migrations")
    assert row["cnt"] > 0


async def test_connect_idempotent(db):
    """Calling connect() again is safe."""
    await db.connect()
    assert await db.health_check() is True


async def test_close_idempotent(db):
    """Calling close() again is safe."""
    await db.close()
    await db.close()
