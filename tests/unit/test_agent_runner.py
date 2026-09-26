from pathlib import Path

import pytest

from agent.runner import AgentRunner
from api.context import get_run_context, get_thread_context
from persistence.artifacts import ArtifactRepository, ArtifactService
from persistence.database import Database
from persistence.repositories import ConversationRepository


@pytest.fixture
async def repository(tmp_path: Path):
    database = Database(tmp_path / "application.db")
    await database.connect()
    try:
        yield ConversationRepository(database)
    finally:
        await database.close()


@pytest.mark.unit
async def test_runner_persists_success_and_assistant_message(repository) -> None:
    submission = await repository.submit("thread-a", "research", "request-a")

    async def execute(query: str, thread_id: str, *, agent) -> str:
        assert (query, thread_id, agent) == ("research", "thread-a", "fake-agent")
        assert get_run_context() == submission.run.id
        assert get_thread_context() == "thread-a"
        running = await repository.get_run(submission.run.id)
        assert running.status == "running"
        return "final answer"

    runner = AgentRunner(repository, agent="fake-agent", execute=execute)
    await runner.run(submission.run.id)

    run = await repository.get_run(submission.run.id)
    messages = await repository.list_messages("thread-a")
    assert run.status == "succeeded"
    assert run.started_at is not None
    assert run.finished_at is not None
    assert [(message.role, message.content) for message in messages] == [
        ("user", "research"),
        ("assistant", "final answer"),
    ]
    assert get_run_context() is None
    assert get_thread_context() is None


@pytest.mark.unit
async def test_runner_persists_failure_and_reraises(repository) -> None:
    submission = await repository.submit("thread-a", "research", "request-a")

    async def execute(query: str, thread_id: str, *, agent) -> str:
        raise RuntimeError("model unavailable")

    runner = AgentRunner(repository, agent="fake-agent", execute=execute)

    with pytest.raises(RuntimeError, match="model unavailable"):
        await runner.run(submission.run.id)

    run = await repository.get_run(submission.run.id)
    assert run.status == "failed"
    assert run.error_code == "RuntimeError"
    assert run.error_message == "model unavailable"
    assert run.started_at is not None
    assert run.finished_at is not None
    assert [(message.role, message.content) for message in await repository.list_messages("thread-a")] == [
        ("user", "research")
    ]
    assert get_run_context() is None
    assert get_thread_context() is None


@pytest.mark.unit
async def test_runner_indexes_generated_files_before_marking_success(
    repository,
    tmp_path: Path,
) -> None:
    submission = await repository.submit("thread-a", "research", "request-a")
    output_root = tmp_path / "output"
    upload_root = tmp_path / "upload"
    session_dir = output_root / "session_thread-a"
    session_dir.mkdir(parents=True)
    upload_root.mkdir()
    (session_dir / "report.md").write_text("report", encoding="utf-8")
    artifacts = ArtifactRepository(repository.database)
    artifact_service = ArtifactService(
        artifacts,
        upload_root=upload_root,
        output_root=output_root,
    )

    async def execute(query: str, thread_id: str, *, agent) -> str:
        return "final answer"

    runner = AgentRunner(
        repository,
        agent="fake-agent",
        execute=execute,
        artifact_service=artifact_service,
    )
    await runner.run(submission.run.id)

    indexed = await artifacts.list_for_thread("thread-a")
    assert [(item.run_id, item.kind, item.filename) for item in indexed] == [
        (submission.run.id, "generated", "report.md")
    ]
