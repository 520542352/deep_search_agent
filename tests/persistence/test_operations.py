from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from persistence.database import Database
from persistence.operations import (
    BackupVerificationError,
    RestoreTargetError,
    check_persistence_health,
    create_backup,
    prune_archived_threads,
    restore_backup,
    verify_backup,
)


def _create_checkpoint_database(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        PRAGMA journal_mode = WAL;
        CREATE TABLE checkpoints (
            thread_id TEXT NOT NULL,
            checkpoint_ns TEXT NOT NULL DEFAULT '',
            checkpoint_id TEXT NOT NULL,
            parent_checkpoint_id TEXT,
            type TEXT,
            checkpoint BLOB,
            metadata BLOB,
            PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id)
        );
        CREATE TABLE writes (
            thread_id TEXT NOT NULL,
            checkpoint_ns TEXT NOT NULL DEFAULT '',
            checkpoint_id TEXT NOT NULL,
            task_id TEXT NOT NULL,
            idx INTEGER NOT NULL,
            channel TEXT NOT NULL,
            type TEXT,
            value BLOB,
            PRIMARY KEY (
                thread_id, checkpoint_ns, checkpoint_id, task_id, idx
            )
        );
        """
    )
    return connection


@pytest.mark.unit
async def test_backup_captures_live_wal_databases_and_artifacts(tmp_path: Path) -> None:
    application_path = tmp_path / "data" / "application.db"
    database = Database(application_path)
    await database.connect()
    await database.connection.execute("INSERT INTO threads (id) VALUES ('thread-a')")
    await database.connection.commit()

    checkpoint_path = tmp_path / "data" / "checkpoints.db"
    checkpoint_connection = _create_checkpoint_database(checkpoint_path)
    checkpoint_connection.execute(
        """
        INSERT INTO checkpoints (
            thread_id, checkpoint_id, type, checkpoint, metadata
        ) VALUES ('thread-a', 'checkpoint-a', 'json', X'01', X'02')
        """
    )
    checkpoint_connection.commit()

    upload = tmp_path / "upload" / "session_thread-a" / "source.txt"
    output = tmp_path / "output" / "session_thread-a" / "report.md"
    upload.parent.mkdir(parents=True)
    output.parent.mkdir(parents=True)
    upload.write_text("source", encoding="utf-8")
    (upload.parent / "notes-wal").write_text("user data", encoding="utf-8")
    output.write_text("report", encoding="utf-8")

    try:
        bundle = create_backup(
            application_path=application_path,
            checkpoint_path=checkpoint_path,
            upload_root=tmp_path / "upload",
            output_root=tmp_path / "output",
            destination_root=tmp_path / "backups",
            now=datetime(2026, 10, 8, 1, 2, 3, tzinfo=UTC),
        )
    finally:
        checkpoint_connection.close()
        await database.close()

    assert bundle.name == "deep-search-20261008T010203Z"
    with sqlite3.connect(bundle / "databases" / "application.db") as connection:
        assert connection.execute("SELECT id FROM threads").fetchone() == (
            "thread-a",
        )
    with sqlite3.connect(bundle / "databases" / "checkpoints.db") as connection:
        assert connection.execute(
            "SELECT checkpoint_id FROM checkpoints"
        ).fetchone() == ("checkpoint-a",)
    assert (bundle / "files" / "upload" / "session_thread-a" / "source.txt").read_text(
        encoding="utf-8"
    ) == "source"
    assert (bundle / "files" / "output" / "session_thread-a" / "report.md").read_text(
        encoding="utf-8"
    ) == "report"

    manifest = verify_backup(bundle)
    assert manifest["format_version"] == 1
    assert manifest["databases"] == {
        "application": "databases/application.db",
        "checkpoints": "databases/checkpoints.db",
    }
    assert set(manifest["files"]) == {
        "databases/application.db",
        "databases/checkpoints.db",
        "files/output/session_thread-a/report.md",
        "files/upload/session_thread-a/notes-wal",
        "files/upload/session_thread-a/source.txt",
    }


@pytest.mark.unit
async def test_backup_verification_detects_file_tampering(tmp_path: Path) -> None:
    application_path = tmp_path / "application.db"
    database = Database(application_path)
    await database.connect()
    await database.close()
    checkpoint_path = tmp_path / "checkpoints.db"
    _create_checkpoint_database(checkpoint_path).close()

    bundle = create_backup(
        application_path=application_path,
        checkpoint_path=checkpoint_path,
        upload_root=tmp_path / "missing-upload",
        output_root=tmp_path / "missing-output",
        destination_root=tmp_path / "backups",
        now=datetime(2026, 10, 8, tzinfo=UTC),
    )
    (bundle / "databases" / "application.db").write_bytes(b"tampered")

    with pytest.raises(BackupVerificationError, match="checksum mismatch"):
        verify_backup(bundle)


@pytest.mark.unit
async def test_restore_writes_verified_backup_to_empty_directory(tmp_path: Path) -> None:
    application_path = tmp_path / "application.db"
    database = Database(application_path)
    await database.connect()
    await database.connection.execute("INSERT INTO threads (id) VALUES ('thread-a')")
    await database.connection.commit()
    await database.close()
    checkpoint_path = tmp_path / "checkpoints.db"
    _create_checkpoint_database(checkpoint_path).close()
    source_file = tmp_path / "upload" / "session_thread-a" / "source.txt"
    source_file.parent.mkdir(parents=True)
    source_file.write_text("source", encoding="utf-8")

    bundle = create_backup(
        application_path=application_path,
        checkpoint_path=checkpoint_path,
        upload_root=tmp_path / "upload",
        output_root=tmp_path / "output",
        destination_root=tmp_path / "backups",
        now=datetime(2026, 10, 8, tzinfo=UTC),
    )
    restore_root = tmp_path / "restored"

    restored = restore_backup(bundle, restore_root)

    assert restored == restore_root
    assert (restore_root / "data" / "application.db").is_file()
    assert (restore_root / "data" / "checkpoints.db").is_file()
    assert (
        restore_root / "upload" / "session_thread-a" / "source.txt"
    ).read_text(encoding="utf-8") == "source"
    with sqlite3.connect(restore_root / "data" / "application.db") as connection:
        assert connection.execute("SELECT id FROM threads").fetchone() == (
            "thread-a",
        )

    with pytest.raises(RestoreTargetError, match="empty"):
        restore_backup(bundle, restore_root)


@pytest.mark.unit
async def test_health_reports_integrity_schema_and_required_tables(tmp_path: Path) -> None:
    application_path = tmp_path / "application.db"
    database = Database(application_path)
    await database.connect()
    await database.close()
    checkpoint_path = tmp_path / "checkpoints.db"
    _create_checkpoint_database(checkpoint_path).close()

    health = check_persistence_health(application_path, checkpoint_path)

    assert health.healthy is True
    assert health.application.integrity == "ok"
    assert health.application.schema_version == 2
    assert health.checkpoints.integrity == "ok"
    assert health.checkpoints.missing_tables == ()


@pytest.mark.unit
async def test_health_rejects_an_outdated_application_schema(tmp_path: Path) -> None:
    application_path = tmp_path / "application.db"
    database = Database(application_path)
    await database.connect()
    await database.connection.execute(
        "DELETE FROM schema_migrations WHERE version = 2"
    )
    await database.connection.commit()
    await database.close()
    checkpoint_path = tmp_path / "checkpoints.db"
    _create_checkpoint_database(checkpoint_path).close()

    health = check_persistence_health(application_path, checkpoint_path)

    assert health.healthy is False
    assert health.application.schema_version == 1
    assert health.application.expected_schema_version == 2


@pytest.mark.unit
async def test_prune_defaults_to_dry_run_and_only_removes_old_archived_threads(
    tmp_path: Path,
) -> None:
    application_path = tmp_path / "application.db"
    database = Database(application_path)
    await database.connect()
    await database.connection.executemany(
        """
        INSERT INTO threads (id, status, updated_at) VALUES (?, ?, ?)
        """,
        (
            ("old-archived", "archived", "2026-08-01T00:00:00.000Z"),
            ("recent-archived", "archived", "2026-10-07T00:00:00.000Z"),
            ("old-active", "active", "2026-08-01T00:00:00.000Z"),
        ),
    )
    await database.connection.execute(
        """
        INSERT INTO runs (id, thread_id, query, status)
        VALUES ('old-run', 'old-archived', 'old query', 'succeeded')
        """
    )
    await database.connection.execute(
        """
        INSERT INTO messages (id, thread_id, run_id, role, content, sequence)
        VALUES ('old-message', 'old-archived', 'old-run', 'user', 'old query', 0)
        """
    )
    await database.connection.execute(
        """
        INSERT INTO events (thread_id, run_id, event_type)
        VALUES ('old-archived', 'old-run', 'done')
        """
    )
    await database.connection.execute(
        """
        INSERT INTO artifacts (
            id, thread_id, run_id, kind, filename, relative_path,
            size_bytes, sha256
        ) VALUES (
            'old-artifact', 'old-archived', 'old-run', 'generated',
            'report.md', 'output/session_old-archived/report.md', 6, 'abc123'
        )
        """
    )
    await database.connection.commit()
    await database.close()

    checkpoint_path = tmp_path / "checkpoints.db"
    checkpoint_connection = _create_checkpoint_database(checkpoint_path)
    checkpoint_connection.executemany(
        """
        INSERT INTO checkpoints (
            thread_id, checkpoint_id, type, checkpoint, metadata
        ) VALUES (?, 'checkpoint-a', 'json', X'01', X'02')
        """,
        (("old-archived",), ("recent-archived",), ("old-active",)),
    )
    checkpoint_connection.execute(
        """
        INSERT INTO writes (
            thread_id, checkpoint_id, task_id, idx, channel, type, value
        ) VALUES (
            'old-archived', 'checkpoint-a', 'task-a', 0, 'messages',
            'json', X'01'
        )
        """
    )
    checkpoint_connection.commit()
    checkpoint_connection.close()

    for root_name in ("upload", "output"):
        for thread_id in ("old-archived", "recent-archived", "old-active"):
            session = tmp_path / root_name / f"session_{thread_id}"
            session.mkdir(parents=True)
            (session / "file.txt").write_text(thread_id, encoding="utf-8")

    cutoff = datetime(2026, 10, 1, tzinfo=UTC)
    preview = prune_archived_threads(
        application_path=application_path,
        checkpoint_path=checkpoint_path,
        upload_root=tmp_path / "upload",
        output_root=tmp_path / "output",
        older_than=cutoff,
    )

    assert preview.dry_run is True
    assert preview.thread_ids == ("old-archived",)
    with sqlite3.connect(application_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM threads").fetchone() == (3,)

    applied = prune_archived_threads(
        application_path=application_path,
        checkpoint_path=checkpoint_path,
        upload_root=tmp_path / "upload",
        output_root=tmp_path / "output",
        older_than=cutoff,
        apply=True,
    )

    assert applied.dry_run is False
    assert applied.thread_ids == ("old-archived",)
    with sqlite3.connect(application_path) as connection:
        assert connection.execute(
            "SELECT id FROM threads ORDER BY id"
        ).fetchall() == [("old-active",), ("recent-archived",)]
        for table in ("runs", "messages", "events", "artifacts"):
            assert connection.execute(
                f"SELECT COUNT(*) FROM {table} WHERE thread_id = 'old-archived'"
            ).fetchone() == (0,)
    with sqlite3.connect(checkpoint_path) as connection:
        assert connection.execute(
            "SELECT thread_id FROM checkpoints ORDER BY thread_id"
        ).fetchall() == [("old-active",), ("recent-archived",)]
        assert connection.execute(
            "SELECT COUNT(*) FROM writes WHERE thread_id = 'old-archived'"
        ).fetchone() == (0,)
    assert not (tmp_path / "upload" / "session_old-archived").exists()
    assert not (tmp_path / "output" / "session_old-archived").exists()
    assert (tmp_path / "upload" / "session_recent-archived").is_dir()
