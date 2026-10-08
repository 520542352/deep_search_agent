from pathlib import Path

import pytest

from persistence.artifacts import ArtifactRepository, ArtifactService
from persistence.database import Database
from persistence.repositories import ConversationRepository, RecordNotFoundError


@pytest.fixture
async def artifact_context(tmp_path: Path):
    database = Database(tmp_path / "application.db")
    await database.connect()
    uploads = tmp_path / "upload"
    outputs = tmp_path / "output"
    uploads.mkdir()
    outputs.mkdir()
    repository = ArtifactRepository(database)
    service = ArtifactService(
        repository,
        upload_root=uploads,
        output_root=outputs,
    )
    try:
        yield database, repository, service, uploads, outputs
    finally:
        await database.close()


@pytest.mark.unit
async def test_register_upload_persists_metadata_and_creates_thread(
    artifact_context,
) -> None:
    _database, repository, service, uploads, _outputs = artifact_context
    session = uploads / "session_thread-a"
    session.mkdir()
    uploaded = session / "note.txt"
    uploaded.write_bytes(b"hello")

    artifact = await service.register_upload(
        "thread-a",
        uploaded,
        media_type="text/plain",
    )

    assert artifact.thread_id == "thread-a"
    assert artifact.run_id is None
    assert artifact.kind == "upload"
    assert artifact.filename == "note.txt"
    assert artifact.relative_path == "upload/session_thread-a/note.txt"
    assert artifact.media_type == "text/plain"
    assert artifact.size_bytes == 5
    assert artifact.sha256 == (
        "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824"
    )
    assert await repository.get(artifact.id) == artifact
    assert await repository.list_for_thread("thread-a") == [artifact]


@pytest.mark.unit
async def test_generated_indexing_skips_unchanged_upload_copy(
    artifact_context,
) -> None:
    database, repository, service, uploads, outputs = artifact_context
    conversations = ConversationRepository(database)
    submission = await conversations.submit("thread-a", "research", "request-a")
    upload_session = uploads / "session_thread-a"
    output_session = outputs / "session_thread-a"
    upload_session.mkdir()
    output_session.mkdir()
    upload = upload_session / "source.txt"
    upload.write_bytes(b"source")
    await service.register_upload("thread-a", upload, media_type="text/plain")
    (output_session / "source.txt").write_bytes(b"source")
    (output_session / "report.md").write_bytes(b"report")

    indexed = await service.index_generated(
        "thread-a",
        submission.run.id,
    )

    assert [artifact.filename for artifact in indexed] == ["report.md"]
    artifacts = await repository.list_for_thread("thread-a")
    assert [(item.kind, item.filename) for item in artifacts] == [
        ("generated", "report.md"),
        ("upload", "source.txt"),
    ]
    assert indexed[0].run_id == submission.run.id
    assert indexed[0].media_type == "text/markdown"


@pytest.mark.unit
async def test_artifact_resolution_is_confined_to_configured_roots(
    artifact_context,
) -> None:
    _database, repository, service, uploads, _outputs = artifact_context
    session = uploads / "session_thread-a"
    session.mkdir()
    uploaded = session / "note.txt"
    uploaded.write_bytes(b"hello")
    artifact = await service.register_upload("thread-a", uploaded)

    assert service.resolve(artifact) == uploaded.resolve()

    await repository.database.connection.execute(
        "UPDATE artifacts SET relative_path = ? WHERE id = ?",
        ("upload/../secret.txt", artifact.id),
    )
    await repository.database.connection.commit()
    tampered = await repository.get(artifact.id)
    with pytest.raises(ValueError, match="artifact path"):
        service.resolve(tampered)


@pytest.mark.unit
async def test_loading_missing_artifact_raises_not_found(artifact_context) -> None:
    _database, repository, _service, _uploads, _outputs = artifact_context

    with pytest.raises(RecordNotFoundError, match="artifact not found"):
        await repository.get("missing")
