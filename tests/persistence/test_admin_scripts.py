from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from persistence.database import Database
from scripts.backup_sqlite import main as backup_main
from scripts.prune_persistence import main as prune_main


def _create_checkpoint_database(path: Path) -> None:
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
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


@pytest.mark.unit
async def test_backup_health_command_prints_machine_readable_result(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    application_path = tmp_path / "application.db"
    database = Database(application_path)
    await database.connect()
    await database.close()
    checkpoint_path = tmp_path / "checkpoints.db"
    _create_checkpoint_database(checkpoint_path)

    exit_code = backup_main(
        [
            "health",
            "--application-db",
            str(application_path),
            "--checkpoint-db",
            str(checkpoint_path),
        ]
    )

    result = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert result["healthy"] is True
    assert result["application"]["schema_version"] == 2


@pytest.mark.unit
async def test_prune_command_is_a_dry_run_without_apply(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    application_path = tmp_path / "application.db"
    database = Database(application_path)
    await database.connect()
    await database.connection.execute(
        """
        INSERT INTO threads (id, status, updated_at)
        VALUES ('old-archived', 'archived', '2020-01-01T00:00:00.000Z')
        """
    )
    await database.connection.commit()
    await database.close()
    checkpoint_path = tmp_path / "checkpoints.db"
    _create_checkpoint_database(checkpoint_path)

    exit_code = prune_main(
        [
            "--application-db",
            str(application_path),
            "--checkpoint-db",
            str(checkpoint_path),
            "--upload-root",
            str(tmp_path / "upload"),
            "--output-root",
            str(tmp_path / "output"),
            "--older-than-days",
            "30",
        ]
    )

    result = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert result["dry_run"] is True
    assert result["thread_ids"] == ["old-archived"]
    with sqlite3.connect(application_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM threads").fetchone() == (1,)
