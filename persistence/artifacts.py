"""Persistent artifact metadata and safe filesystem resolution."""

from __future__ import annotations

import hashlib
import mimetypes
import sqlite3
import uuid
from dataclasses import dataclass
from pathlib import Path

from persistence.database import Database
from persistence.repositories import RecordNotFoundError


@dataclass(frozen=True)
class ArtifactRecord:
    id: str
    thread_id: str
    run_id: str | None
    kind: str
    filename: str
    relative_path: str
    media_type: str | None
    size_bytes: int
    sha256: str
    created_at: str


def _artifact_from_row(row: sqlite3.Row) -> ArtifactRecord:
    return ArtifactRecord(**dict(row))


class ArtifactRepository:
    def __init__(self, database: Database):
        self.database = database

    async def register(
        self,
        *,
        thread_id: str,
        run_id: str | None,
        kind: str,
        filename: str,
        relative_path: str,
        media_type: str | None,
        size_bytes: int,
        sha256: str,
    ) -> ArtifactRecord:
        artifact_id = str(uuid.uuid4())
        async with self.database.write_transaction() as connection:
            await connection.execute(
                """
                INSERT INTO threads (id) VALUES (?)
                ON CONFLICT(id) DO UPDATE SET
                    updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                """,
                (thread_id,),
            )
            await connection.execute(
                """
                INSERT INTO artifacts (
                    id, thread_id, run_id, kind, filename, relative_path,
                    media_type, size_bytes, sha256
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(thread_id, relative_path) DO UPDATE SET
                    run_id = CASE
                        WHEN artifacts.sha256 = excluded.sha256
                        THEN artifacts.run_id
                        ELSE excluded.run_id
                    END,
                    kind = excluded.kind,
                    filename = excluded.filename,
                    media_type = excluded.media_type,
                    size_bytes = excluded.size_bytes,
                    sha256 = excluded.sha256,
                    created_at = CASE
                        WHEN artifacts.sha256 = excluded.sha256
                        THEN artifacts.created_at
                        ELSE strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                    END
                """,
                (
                    artifact_id,
                    thread_id,
                    run_id,
                    kind,
                    filename,
                    relative_path,
                    media_type,
                    size_bytes,
                    sha256,
                ),
            )
            row = await (
                await connection.execute(
                    """
                    SELECT * FROM artifacts
                    WHERE thread_id = ? AND relative_path = ?
                    """,
                    (thread_id, relative_path),
                )
            ).fetchone()
        return _artifact_from_row(row)

    async def get(self, artifact_id: str) -> ArtifactRecord:
        row = await (
            await self.database.connection.execute(
                "SELECT * FROM artifacts WHERE id = ?",
                (artifact_id,),
            )
        ).fetchone()
        if row is None:
            raise RecordNotFoundError(f"artifact not found: {artifact_id}")
        return _artifact_from_row(row)

    async def list_for_thread(self, thread_id: str) -> list[ArtifactRecord]:
        rows = await (
            await self.database.connection.execute(
                """
                SELECT * FROM artifacts
                WHERE thread_id = ?
                ORDER BY created_at DESC, rowid DESC
                """,
                (thread_id,),
            )
        ).fetchall()
        return [_artifact_from_row(row) for row in rows]


class ArtifactService:
    def __init__(
        self,
        repository: ArtifactRepository,
        *,
        upload_root: Path,
        output_root: Path,
    ) -> None:
        self.repository = repository
        self.upload_root = Path(upload_root)
        self.output_root = Path(output_root)

    async def register_upload(
        self,
        thread_id: str,
        path: Path,
        *,
        media_type: str | None = None,
    ) -> ArtifactRecord:
        return await self._register_file(
            thread_id=thread_id,
            run_id=None,
            kind="upload",
            path=path,
            media_type=media_type,
        )

    async def index_generated(
        self,
        thread_id: str,
        run_id: str,
    ) -> list[ArtifactRecord]:
        session_dir = self.output_root / f"session_{thread_id}"
        if not session_dir.is_dir():
            return []

        uploads = await self.repository.list_for_thread(thread_id)
        upload_hashes = {
            artifact.filename: artifact.sha256
            for artifact in uploads
            if artifact.kind == "upload"
        }
        indexed: list[ArtifactRecord] = []
        for path in sorted(item for item in session_dir.rglob("*") if item.is_file()):
            digest = _sha256(path)
            if path.parent == session_dir and upload_hashes.get(path.name) == digest:
                continue
            indexed.append(
                await self._register_file(
                    thread_id=thread_id,
                    run_id=run_id,
                    kind="generated",
                    path=path,
                    media_type=mimetypes.guess_type(path.name)[0],
                    sha256=digest,
                )
            )
        return indexed

    def resolve(self, artifact: ArtifactRecord) -> Path:
        prefixes = {
            "upload": ("upload", self.upload_root),
            "generated": ("output", self.output_root),
        }
        try:
            expected_prefix, root = prefixes[artifact.kind]
        except KeyError as exc:
            raise ValueError("invalid artifact path") from exc

        logical_path = Path(artifact.relative_path)
        if (
            logical_path.is_absolute()
            or not logical_path.parts
            or logical_path.parts[0] != expected_prefix
        ):
            raise ValueError("invalid artifact path")
        candidate = root.joinpath(*logical_path.parts[1:]).resolve()
        try:
            candidate.relative_to(root.resolve())
        except ValueError as exc:
            raise ValueError("invalid artifact path") from exc
        return candidate

    async def _register_file(
        self,
        *,
        thread_id: str,
        run_id: str | None,
        kind: str,
        path: Path,
        media_type: str | None,
        sha256: str | None = None,
    ) -> ArtifactRecord:
        root = self.upload_root if kind == "upload" else self.output_root
        resolved_path = Path(path).resolve()
        session_root = (root / f"session_{thread_id}").resolve()
        try:
            relative_to_root = resolved_path.relative_to(root.resolve())
            resolved_path.relative_to(session_root)
        except ValueError as exc:
            raise ValueError("invalid artifact path") from exc
        stat = resolved_path.stat()
        prefix = "upload" if kind == "upload" else "output"
        return await self.repository.register(
            thread_id=thread_id,
            run_id=run_id,
            kind=kind,
            filename=resolved_path.name,
            relative_path=(Path(prefix) / relative_to_root).as_posix(),
            media_type=media_type,
            size_bytes=stat.st_size,
            sha256=sha256 or _sha256(resolved_path),
        )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
