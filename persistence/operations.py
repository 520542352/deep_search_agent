"""Offline operations for backing up, checking, restoring, and pruning SQLite data."""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
from contextlib import closing
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any


APPLICATION_TABLES = frozenset(
    {"artifacts", "events", "messages", "runs", "schema_migrations", "threads"}
)
CHECKPOINT_TABLES = frozenset({"checkpoints", "writes"})
BACKUP_FORMAT_VERSION = 1
APPLICATION_SCHEMA_VERSION = 2


class PersistenceOperationError(RuntimeError):
    """Base error for persistence administration failures."""


class BackupVerificationError(PersistenceOperationError):
    """Raised when a backup bundle is incomplete, corrupt, or unsafe."""


class RestoreTargetError(PersistenceOperationError):
    """Raised when a restore target could overwrite existing data."""


@dataclass(frozen=True)
class DatabaseHealth:
    path: str
    exists: bool
    integrity: str
    schema_version: int | None
    expected_schema_version: int | None
    missing_tables: tuple[str, ...]
    error: str | None = None

    @property
    def healthy(self) -> bool:
        return (
            self.exists
            and self.integrity == "ok"
            and not self.missing_tables
            and self.error is None
            and (
                self.expected_schema_version is None
                or self.schema_version == self.expected_schema_version
            )
        )


@dataclass(frozen=True)
class PersistenceHealth:
    application: DatabaseHealth
    checkpoints: DatabaseHealth

    @property
    def healthy(self) -> bool:
        return self.application.healthy and self.checkpoints.healthy

    def to_dict(self) -> dict[str, Any]:
        return {
            "healthy": self.healthy,
            "application": {
                **asdict(self.application),
                "healthy": self.application.healthy,
            },
            "checkpoints": {
                **asdict(self.checkpoints),
                "healthy": self.checkpoints.healthy,
            },
        }


@dataclass(frozen=True)
class PruneResult:
    dry_run: bool
    older_than: str
    thread_ids: tuple[str, ...]
    removed_paths: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _utc_timestamp(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("datetime must include a timezone")
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _is_transient_sqlite_sidecar(relative_path: str) -> bool:
    return relative_path in {
        "databases/application.db-shm",
        "databases/application.db-wal",
        "databases/checkpoints.db-shm",
        "databases/checkpoints.db-wal",
    }


def _sqlite_backup(source_path: Path, destination_path: Path) -> None:
    if not source_path.is_file():
        raise FileNotFoundError(f"SQLite database does not exist: {source_path}")
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(source_path)) as source, closing(
        sqlite3.connect(destination_path)
    ) as destination:
        source.backup(destination)


def _copy_tree_if_present(source: Path, destination: Path) -> None:
    if source.exists() and not source.is_dir():
        raise PersistenceOperationError(f"artifact root is not a directory: {source}")
    if source.is_dir():
        shutil.copytree(source, destination)


def _manifest_files(bundle: Path) -> dict[str, dict[str, int | str]]:
    files: dict[str, dict[str, int | str]] = {}
    for path in sorted(bundle.rglob("*")):
        relative_path = path.relative_to(bundle).as_posix()
        if (
            path.is_file()
            and path.name != "manifest.json"
            and not _is_transient_sqlite_sidecar(relative_path)
        ):
            files[relative_path] = {
                "sha256": _sha256(path),
                "size_bytes": path.stat().st_size,
            }
    return files


def create_backup(
    *,
    application_path: Path,
    checkpoint_path: Path,
    upload_root: Path,
    output_root: Path,
    destination_root: Path,
    now: datetime | None = None,
) -> Path:
    """Create a verified backup bundle without raw-copying live SQLite files."""
    created_at = datetime.now(UTC) if now is None else now.astimezone(UTC)
    bundle_name = f"deep-search-{created_at.strftime('%Y%m%dT%H%M%SZ')}"
    destination_root = Path(destination_root)
    bundle = destination_root / bundle_name
    staging = destination_root / f".{bundle_name}.incomplete"
    destination_root.mkdir(parents=True, exist_ok=True)
    if bundle.exists() or staging.exists():
        raise FileExistsError(f"backup destination already exists: {bundle}")

    staging.mkdir()
    try:
        _sqlite_backup(
            Path(application_path), staging / "databases" / "application.db"
        )
        _sqlite_backup(
            Path(checkpoint_path), staging / "databases" / "checkpoints.db"
        )
        _copy_tree_if_present(Path(upload_root), staging / "files" / "upload")
        _copy_tree_if_present(Path(output_root), staging / "files" / "output")
        manifest: dict[str, Any] = {
            "format_version": BACKUP_FORMAT_VERSION,
            "created_at": created_at.isoformat().replace("+00:00", "Z"),
            "databases": {
                "application": "databases/application.db",
                "checkpoints": "databases/checkpoints.db",
            },
            "files": _manifest_files(staging),
        }
        (staging / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        verify_backup(staging)
        staging.replace(bundle)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return bundle


def _safe_manifest_path(bundle: Path, relative_path: str) -> Path:
    pure_path = PurePosixPath(relative_path)
    if pure_path.is_absolute() or ".." in pure_path.parts or not pure_path.parts:
        raise BackupVerificationError(f"unsafe manifest path: {relative_path}")
    candidate = bundle.joinpath(*pure_path.parts).resolve()
    try:
        candidate.relative_to(bundle.resolve())
    except ValueError as exc:
        raise BackupVerificationError(
            f"manifest path escapes backup bundle: {relative_path}"
        ) from exc
    return candidate


def _quick_check(path: Path) -> str:
    try:
        with closing(
            sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
        ) as connection:
            row = connection.execute("PRAGMA quick_check").fetchone()
    except sqlite3.DatabaseError as exc:
        raise BackupVerificationError(
            f"SQLite integrity check failed for {path}: {exc}"
        ) from exc
    return "" if row is None else str(row[0])


def verify_backup(bundle: Path) -> dict[str, Any]:
    """Verify bundle shape, file checksums, and both SQLite snapshots."""
    bundle = Path(bundle)
    manifest_path = bundle / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BackupVerificationError(f"cannot read backup manifest: {exc}") from exc
    if manifest.get("format_version") != BACKUP_FORMAT_VERSION:
        raise BackupVerificationError("unsupported backup format version")
    files = manifest.get("files")
    databases = manifest.get("databases")
    if not isinstance(files, dict) or not isinstance(databases, dict):
        raise BackupVerificationError("backup manifest is missing file metadata")

    for relative_path, metadata in files.items():
        if not isinstance(relative_path, str) or not isinstance(metadata, dict):
            raise BackupVerificationError("backup manifest contains invalid file metadata")
        path = _safe_manifest_path(bundle, relative_path)
        if not path.is_file():
            raise BackupVerificationError(f"backup file is missing: {relative_path}")
        if _sha256(path) != metadata.get("sha256"):
            raise BackupVerificationError(f"checksum mismatch: {relative_path}")
        if path.stat().st_size != metadata.get("size_bytes"):
            raise BackupVerificationError(f"size mismatch: {relative_path}")

    actual_files: set[str] = set()
    for path in bundle.rglob("*"):
        relative_path = path.relative_to(bundle).as_posix()
        if (
            path.is_file()
            and path.name != "manifest.json"
            and not _is_transient_sqlite_sidecar(relative_path)
        ):
            actual_files.add(relative_path)
    if actual_files != set(files):
        raise BackupVerificationError("backup contains untracked files")

    for database_name in ("application", "checkpoints"):
        relative_path = databases.get(database_name)
        if not isinstance(relative_path, str) or relative_path not in files:
            raise BackupVerificationError(
                f"backup manifest is missing {database_name} database"
            )
        database_path = _safe_manifest_path(bundle, relative_path)
        if _quick_check(database_path) != "ok":
            raise BackupVerificationError(
                f"SQLite integrity check returned errors for {relative_path}"
            )
    return manifest


def restore_backup(bundle: Path, restore_root: Path) -> Path:
    """Restore a verified bundle into a new or empty directory."""
    bundle = Path(bundle).resolve()
    restore_root = Path(restore_root).resolve()
    if restore_root == bundle or bundle in restore_root.parents:
        raise RestoreTargetError("restore target must be outside the backup bundle")
    if restore_root.exists() and any(restore_root.iterdir()):
        raise RestoreTargetError("restore target must be empty")

    manifest = verify_backup(bundle)
    restore_root.mkdir(parents=True, exist_ok=True)
    try:
        databases = manifest["databases"]
        _sqlite_backup(
            _safe_manifest_path(bundle, databases["application"]),
            restore_root / "data" / "application.db",
        )
        _sqlite_backup(
            _safe_manifest_path(bundle, databases["checkpoints"]),
            restore_root / "data" / "checkpoints.db",
        )
        for directory_name in ("upload", "output"):
            source = bundle / "files" / directory_name
            if source.is_dir():
                shutil.copytree(source, restore_root / directory_name)
        shutil.copy2(bundle / "manifest.json", restore_root / "restore-manifest.json")
    except BaseException:
        shutil.rmtree(restore_root, ignore_errors=True)
        raise
    return restore_root


def _database_health(
    path: Path,
    *,
    required_tables: frozenset[str],
    include_schema_version: bool,
) -> DatabaseHealth:
    path = Path(path)
    if not path.is_file():
        return DatabaseHealth(
            path=str(path),
            exists=False,
            integrity="missing",
            schema_version=None,
            expected_schema_version=(
                APPLICATION_SCHEMA_VERSION if include_schema_version else None
            ),
            missing_tables=tuple(sorted(required_tables)),
            error="database file does not exist",
        )
    try:
        with closing(
            sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
        ) as connection:
            integrity_row = connection.execute("PRAGMA quick_check").fetchone()
            integrity = "" if integrity_row is None else str(integrity_row[0])
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            schema_version = None
            if include_schema_version and "schema_migrations" in tables:
                row = connection.execute(
                    "SELECT MAX(version) FROM schema_migrations"
                ).fetchone()
                schema_version = None if row is None else row[0]
        return DatabaseHealth(
            path=str(path),
            exists=True,
            integrity=integrity,
            schema_version=schema_version,
            expected_schema_version=(
                APPLICATION_SCHEMA_VERSION if include_schema_version else None
            ),
            missing_tables=tuple(sorted(required_tables - tables)),
        )
    except sqlite3.DatabaseError as exc:
        return DatabaseHealth(
            path=str(path),
            exists=True,
            integrity="error",
            schema_version=None,
            expected_schema_version=(
                APPLICATION_SCHEMA_VERSION if include_schema_version else None
            ),
            missing_tables=tuple(sorted(required_tables)),
            error=str(exc),
        )


def check_persistence_health(
    application_path: Path,
    checkpoint_path: Path,
) -> PersistenceHealth:
    return PersistenceHealth(
        application=_database_health(
            Path(application_path),
            required_tables=APPLICATION_TABLES,
            include_schema_version=True,
        ),
        checkpoints=_database_health(
            Path(checkpoint_path),
            required_tables=CHECKPOINT_TABLES,
            include_schema_version=False,
        ),
    )


def _session_path(root: Path, thread_id: str) -> Path:
    root = root.resolve()
    path = (root / f"session_{thread_id}").resolve()
    if path.parent != root:
        raise PersistenceOperationError(f"unsafe persisted thread id: {thread_id}")
    return path


def prune_archived_threads(
    *,
    application_path: Path,
    checkpoint_path: Path,
    upload_root: Path,
    output_root: Path,
    older_than: datetime,
    apply: bool = False,
) -> PruneResult:
    """Delete old archived threads; preview changes unless ``apply`` is true."""
    cutoff = _utc_timestamp(older_than)
    application_path = Path(application_path)
    checkpoint_path = Path(checkpoint_path)
    with closing(sqlite3.connect(application_path)) as application:
        application.execute("PRAGMA foreign_keys = ON")
        rows = application.execute(
            """
            SELECT id
            FROM threads
            WHERE status = 'archived'
              AND updated_at < ?
              AND NOT EXISTS (
                  SELECT 1 FROM runs
                  WHERE runs.thread_id = threads.id
                    AND runs.status IN ('queued', 'running')
              )
            ORDER BY id
            """,
            (cutoff,),
        ).fetchall()
        thread_ids = tuple(str(row[0]) for row in rows)
        for thread_id in thread_ids:
            _session_path(Path(upload_root), thread_id)
            _session_path(Path(output_root), thread_id)
        if not apply or not thread_ids:
            return PruneResult(
                dry_run=not apply,
                older_than=cutoff,
                thread_ids=thread_ids,
                removed_paths=(),
            )
        application.execute("BEGIN IMMEDIATE")
        application.executemany(
            "DELETE FROM threads WHERE id = ?",
            ((thread_id,) for thread_id in thread_ids),
        )
        application.commit()

    if checkpoint_path.is_file():
        with closing(sqlite3.connect(checkpoint_path)) as checkpoints:
            checkpoint_tables = {
                row[0]
                for row in checkpoints.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            checkpoints.execute("BEGIN IMMEDIATE")
            if "writes" in checkpoint_tables:
                checkpoints.executemany(
                    "DELETE FROM writes WHERE thread_id = ?",
                    ((thread_id,) for thread_id in thread_ids),
                )
            if "checkpoints" in checkpoint_tables:
                checkpoints.executemany(
                    "DELETE FROM checkpoints WHERE thread_id = ?",
                    ((thread_id,) for thread_id in thread_ids),
                )
            checkpoints.commit()

    removed_paths: list[str] = []
    for thread_id in thread_ids:
        for root in (Path(upload_root), Path(output_root)):
            path = _session_path(root, thread_id)
            if path.is_dir():
                shutil.rmtree(path)
                removed_paths.append(str(path))
    return PruneResult(
        dry_run=False,
        older_than=cutoff,
        thread_ids=thread_ids,
        removed_paths=tuple(removed_paths),
    )
