import asyncio
import sqlite3
from pathlib import Path

import pytest

from persistence.database import (
    Database,
    DatabaseNotConnectedError,
    MigrationError,
    resolve_database_path,
)


EXPECTED_TABLES = {
    "artifacts",
    "events",
    "messages",
    "runs",
    "schema_migrations",
    "threads",
}


async def _table_names(database: Database) -> set[str]:
    cursor = await database.connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'"
    )
    rows = await cursor.fetchall()
    return {row[0] for row in rows if not row[0].startswith("sqlite_")}


@pytest.mark.unit
async def test_database_initializes_schema_and_pragmas(tmp_path: Path) -> None:
    database_path = tmp_path / "nested" / "application.db"
    database = Database(database_path)

    await database.connect()
    try:
        assert database_path.is_file()
        assert await _table_names(database) == EXPECTED_TABLES

        foreign_keys = await (
            await database.connection.execute("PRAGMA foreign_keys")
        ).fetchone()
        journal_mode = await (
            await database.connection.execute("PRAGMA journal_mode")
        ).fetchone()
        busy_timeout = await (
            await database.connection.execute("PRAGMA busy_timeout")
        ).fetchone()
        run_columns = {
            row["name"]
            for row in await (
                await database.connection.execute("PRAGMA table_info(runs)")
            ).fetchall()
        }

        assert foreign_keys[0] == 1
        assert journal_mode[0] == "wal"
        assert busy_timeout[0] == 5_000
        assert "base_checkpoint_id" in run_columns
    finally:
        await database.close()


@pytest.mark.unit
async def test_database_migrations_are_idempotent(tmp_path: Path) -> None:
    database_path = tmp_path / "application.db"

    first = Database(database_path)
    await first.connect()
    await first.close()

    second = Database(database_path)
    await second.connect()
    try:
        row = await (
            await second.connection.execute("SELECT COUNT(*) FROM schema_migrations")
        ).fetchone()
        assert row[0] == 2
    finally:
        await second.close()


@pytest.mark.unit
async def test_failed_migration_is_rolled_back_and_connection_is_closed(
    tmp_path: Path,
) -> None:
    migrations_dir = tmp_path / "migrations"
    migrations_dir.mkdir()
    (migrations_dir / "001_broken.sql").write_text(
        "CREATE TABLE partial_write (id INTEGER PRIMARY KEY);\nBROKEN SQL;",
        encoding="utf-8",
    )
    database_path = tmp_path / "application.db"
    database = Database(database_path, migrations_dir=migrations_dir)

    with pytest.raises(MigrationError, match="001_broken.sql"):
        await database.connect()

    with pytest.raises(DatabaseNotConnectedError):
        _ = database.connection

    with sqlite3.connect(database_path) as connection:
        names = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
    assert "partial_write" not in names


@pytest.mark.unit
async def test_migration_versions_must_be_contiguous(tmp_path: Path) -> None:
    migrations_dir = tmp_path / "migrations"
    migrations_dir.mkdir()
    (migrations_dir / "001_first.sql").write_text(
        "CREATE TABLE first_table (id INTEGER PRIMARY KEY);",
        encoding="utf-8",
    )
    (migrations_dir / "003_third.sql").write_text(
        "CREATE TABLE third_table (id INTEGER PRIMARY KEY);",
        encoding="utf-8",
    )
    database = Database(
        tmp_path / "application.db",
        migrations_dir=migrations_dir,
    )

    with pytest.raises(MigrationError, match="missing migration version: 2"):
        await database.connect()

    assert database.is_connected is False


@pytest.mark.unit
async def test_two_database_connections_can_write_without_lock_errors(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "application.db"
    first = Database(database_path)
    second = Database(database_path)
    await first.connect()
    await second.connect()

    async def insert_thread(database: Database, thread_id: str) -> None:
        await database.connection.execute(
            "INSERT INTO threads (id, title) VALUES (?, ?)",
            (thread_id, thread_id),
        )
        await database.connection.commit()

    try:
        await asyncio.gather(
            insert_thread(first, "thread-a"),
            insert_thread(second, "thread-b"),
        )
        row = await (
            await first.connection.execute("SELECT COUNT(*) FROM threads")
        ).fetchone()
        assert row[0] == 2
    finally:
        await first.close()
        await second.close()


@pytest.mark.unit
async def test_write_transactions_are_serialized_on_one_connection(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "application.db")
    await database.connect()
    if not hasattr(database, "write_transaction"):
        await database.close()
        pytest.fail("Database.write_transaction has not been implemented")
    first_entered = asyncio.Event()
    release_first = asyncio.Event()
    order: list[str] = []

    async def first_writer() -> None:
        async with database.write_transaction():
            order.append("first-start")
            first_entered.set()
            await release_first.wait()
            order.append("first-end")

    async def second_writer() -> None:
        await first_entered.wait()
        async with database.write_transaction():
            order.append("second")

    try:
        first = asyncio.create_task(first_writer())
        second = asyncio.create_task(second_writer())
        await first_entered.wait()
        await asyncio.sleep(0)
        assert order == ["first-start"]
        release_first.set()
        await asyncio.gather(first, second)
        assert order == ["first-start", "first-end", "second"]
    finally:
        await database.close()


@pytest.mark.unit
def test_database_rejects_connection_access_before_connect(tmp_path: Path) -> None:
    database = Database(tmp_path / "application.db")

    with pytest.raises(DatabaseNotConnectedError):
        _ = database.connection


@pytest.mark.unit
def test_database_path_can_be_overridden_by_environment(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    configured_path = tmp_path / "state" / "custom.db"

    assert resolve_database_path(project_root, {}) == (
        project_root / "data" / "application.db"
    )
    assert resolve_database_path(
        project_root,
        {"DEEP_SEARCH_DATABASE_PATH": str(configured_path)},
    ) == configured_path
