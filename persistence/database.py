from __future__ import annotations

import os
import re
import sqlite3
from collections.abc import Mapping
from pathlib import Path

import aiosqlite


DEFAULT_BUSY_TIMEOUT_MS = 5_000
DEFAULT_MIGRATIONS_DIR = Path(__file__).with_name("migrations")
MIGRATION_FILENAME = re.compile(r"^(?P<version>\d{3,})_[a-z0-9_]+\.sql$")


def resolve_database_path(
    project_root: Path,
    environ: Mapping[str, str] | None = None,
) -> Path:
    environment = os.environ if environ is None else environ
    configured_path = environment.get("DEEP_SEARCH_DATABASE_PATH")
    if configured_path:
        return Path(configured_path).expanduser()
    return Path(project_root) / "data" / "application.db"


class PersistenceError(RuntimeError):
    """Base error for persistence infrastructure failures."""


class DatabaseNotConnectedError(PersistenceError):
    """Raised when code attempts to use a closed database."""


class MigrationError(PersistenceError):
    """Raised when a schema migration cannot be applied safely."""


class Database:
    def __init__(
        self,
        path: Path,
        *,
        migrations_dir: Path = DEFAULT_MIGRATIONS_DIR,
        busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS,
    ) -> None:
        self.path = Path(path)
        self.migrations_dir = Path(migrations_dir)
        self.busy_timeout_ms = busy_timeout_ms
        self._connection: aiosqlite.Connection | None = None

    @property
    def is_connected(self) -> bool:
        return self._connection is not None

    @property
    def connection(self) -> aiosqlite.Connection:
        if self._connection is None:
            raise DatabaseNotConnectedError("database is not connected")
        return self._connection

    async def connect(self) -> None:
        if self._connection is not None:
            return

        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = await aiosqlite.connect(self.path)
        connection.row_factory = sqlite3.Row
        self._connection = connection

        try:
            await self._configure_connection()
            await self._apply_migrations()
        except Exception:
            await connection.close()
            self._connection = None
            raise

    async def close(self) -> None:
        if self._connection is None:
            return
        connection = self._connection
        self._connection = None
        await connection.close()

    async def _configure_connection(self) -> None:
        await self.connection.execute("PRAGMA foreign_keys = ON")
        await self.connection.execute(f"PRAGMA busy_timeout = {self.busy_timeout_ms}")
        await self.connection.execute("PRAGMA journal_mode = WAL")

    async def _apply_migrations(self) -> None:
        await self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version INTEGER PRIMARY KEY,
                name TEXT NOT NULL UNIQUE,
                applied_at TEXT NOT NULL DEFAULT (
                    strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                )
            )
            """
        )
        await self.connection.commit()

        migrations = self._discover_migrations()
        cursor = await self.connection.execute(
            "SELECT version FROM schema_migrations"
        )
        applied_versions = {row[0] for row in await cursor.fetchall()}

        for version, migration_path in migrations:
            if version in applied_versions:
                continue
            await self._apply_migration(version, migration_path)

    def _discover_migrations(self) -> list[tuple[int, Path]]:
        if not self.migrations_dir.is_dir():
            raise MigrationError(
                f"migration directory does not exist: {self.migrations_dir}"
            )

        migrations: list[tuple[int, Path]] = []
        seen_versions: set[int] = set()
        expected_version = 1
        for path in sorted(self.migrations_dir.glob("*.sql")):
            match = MIGRATION_FILENAME.fullmatch(path.name)
            if match is None:
                raise MigrationError(f"invalid migration filename: {path.name}")
            version = int(match.group("version"))
            if version in seen_versions:
                raise MigrationError(f"duplicate migration version: {version}")
            if version != expected_version:
                raise MigrationError(
                    f"missing migration version: {expected_version}"
                )
            seen_versions.add(version)
            migrations.append((version, path))
            expected_version += 1
        return migrations

    async def _apply_migration(self, version: int, migration_path: Path) -> None:
        migration_sql = migration_path.read_text(encoding="utf-8")
        escaped_name = migration_path.name.replace("'", "''")
        script = (
            "BEGIN IMMEDIATE;\n"
            f"{migration_sql}\n"
            "INSERT INTO schema_migrations (version, name) "
            f"VALUES ({version}, '{escaped_name}');\n"
            "COMMIT;"
        )

        try:
            await self.connection.executescript(script)
        except (sqlite3.DatabaseError, OSError) as exc:
            await self.connection.rollback()
            raise MigrationError(
                f"failed to apply migration {migration_path.name}: {exc}"
            ) from exc
