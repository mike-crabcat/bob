"""SQLite database access and schema migration support."""

from __future__ import annotations

from contextlib import asynccontextmanager
import asyncio
from pathlib import Path
import re
from typing import Any

import aiosqlite


_MIGRATION_PREFIX_RE = re.compile(r"^(\d+)")


class Database:
    """Small async connection pool on top of aiosqlite."""

    def __init__(self, db_path: Path, schema_dir: Path, pool_size: int = 4) -> None:
        self.db_path = db_path
        self.schema_dir = schema_dir
        self.pool_size = max(pool_size, 1)
        self.settings: Any | None = None
        self._queue: asyncio.Queue[aiosqlite.Connection] = asyncio.Queue()
        self._connections: list[aiosqlite.Connection] = []
        self._write_lock = asyncio.Lock()
        self._connected = False

    async def connect(self) -> None:
        """Open the configured SQLite connections."""

        if self._connected:
            return

        self.db_path.parent.mkdir(parents=True, exist_ok=True)

        for _ in range(self.pool_size):
            connection = await aiosqlite.connect(self.db_path.as_posix())
            connection.row_factory = aiosqlite.Row
            await connection.execute("PRAGMA foreign_keys = ON;")
            await connection.execute("PRAGMA journal_mode = WAL;")
            await connection.execute("PRAGMA busy_timeout = 5000;")
            await connection.commit()
            self._connections.append(connection)
            await self._queue.put(connection)

        self._connected = True

    async def close(self) -> None:
        """Close all open connections."""

        if not self._connected:
            return

        while not self._queue.empty():
            await self._queue.get()

        for connection in self._connections:
            await connection.close()

        self._connections.clear()
        self._connected = False

    @asynccontextmanager
    async def connection(self, *, write: bool = False) -> aiosqlite.Connection:
        """Lease a connection from the pool."""

        if not self._connected:
            raise RuntimeError("Database has not been connected")

        connection = await self._queue.get()
        lock_acquired = False
        try:
            if write:
                await self._write_lock.acquire()
                lock_acquired = True
            yield connection
            if write:
                await connection.commit()
        except Exception:
            if write:
                await connection.rollback()
            raise
        finally:
            if lock_acquired:
                self._write_lock.release()
            await self._queue.put(connection)

    async def apply_migrations(self) -> None:
        """Apply SQL migrations from the schema directory once each."""

        async with self.connection(write=True) as connection:
            await connection.execute(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    name TEXT PRIMARY KEY,
                    applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )

            cursor = await connection.execute("SELECT name FROM schema_migrations")
            applied = {row["name"] for row in await cursor.fetchall()}
            await cursor.close()

            for schema_file in sorted(self.schema_dir.glob("*.sql"), key=_migration_sort_key):
                if schema_file.name in applied:
                    continue
                try:
                    await connection.executescript(schema_file.read_text(encoding="utf-8"))
                except Exception as exc:
                    # Gracefully handle migrations that may fail on idempotent runs (e.g., duplicate columns)
                    if "duplicate column" not in str(exc).lower():
                        raise
                await connection.execute(
                    "INSERT INTO schema_migrations (name) VALUES (?)",
                    (schema_file.name,),
                )

    async def fetch_one(self, query: str, params: tuple[Any, ...] = ()) -> dict[str, Any] | None:
        """Execute a read query and return at most one row."""

        async with self.connection() as connection:
            cursor = await connection.execute(query, params)
            row = await cursor.fetchone()
            await cursor.close()
            return dict(row) if row else None

    async def fetch_all(self, query: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        """Execute a read query and return all rows."""

        async with self.connection() as connection:
            cursor = await connection.execute(query, params)
            rows = await cursor.fetchall()
            await cursor.close()
            return [dict(row) for row in rows]

    async def execute(self, query: str, params: tuple[Any, ...] = ()) -> int:
        """Execute a write query and return the affected row count."""

        async with self.connection(write=True) as connection:
            cursor = await connection.execute(query, params)
            rowcount = cursor.rowcount
            await cursor.close()
            return rowcount

    async def execute_many(self, query: str, params: list[tuple[Any, ...]]) -> None:
        """Execute a batch write query."""

        async with self.connection(write=True) as connection:
            await connection.executemany(query, params)

    async def health_check(self) -> bool:
        """Run a trivial query to verify the database is available."""

        row = await self.fetch_one("SELECT 1 AS ok")
        return bool(row and row["ok"] == 1)


def _migration_sort_key(path: Path) -> tuple[int, str]:
    """Sort migrations by leading numeric prefix before falling back to filename."""

    match = _MIGRATION_PREFIX_RE.match(path.name)
    if match is None:
        return (10**9, path.name)
    return (int(match.group(1)), path.name)
