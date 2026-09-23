from pathlib import Path

import pytest

from agent.runner import AgentRunner
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
