import asyncio
from pathlib import Path

import pytest

from persistence.database import Database
from persistence.repositories import (
    ActiveRunConflictError,
    ConversationRepository,
    RecordNotFoundError,
)


@pytest.fixture
async def repository(tmp_path: Path):
    database = Database(tmp_path / "application.db")
    await database.connect()
    try:
        yield ConversationRepository(database)
    finally:
        await database.close()


@pytest.mark.unit
async def test_submit_persists_thread_run_and_original_user_message(repository) -> None:
    submission = await repository.submit(
        thread_id="thread-a",
        query="original question",
        request_id="request-a",
    )

    run = await repository.get_run(submission.run.id)
    messages = await repository.list_messages("thread-a")

    assert submission.created is True
    assert run.thread_id == "thread-a"
    assert run.request_id == "request-a"
    assert run.query == "original question"
    assert run.status == "queued"
    assert [(message.role, message.content, message.run_id) for message in messages] == [
        ("user", "original question", run.id)
    ]


@pytest.mark.unit
async def test_duplicate_request_returns_existing_run_without_duplicate_rows(
    repository,
) -> None:
    first = await repository.submit("thread-a", "research", "request-a")
    duplicate = await repository.submit("thread-a", "research", "request-a")

    assert duplicate.created is False
    assert duplicate.run.id == first.run.id
    assert len(await repository.list_runs("thread-a")) == 1
    assert len(await repository.list_messages("thread-a")) == 1


@pytest.mark.unit
async def test_submit_rejects_another_active_run_for_the_same_thread(repository) -> None:
    await repository.submit("thread-a", "first", "request-a")

    with pytest.raises(ActiveRunConflictError):
        await repository.submit("thread-a", "second", "request-b")

    assert len(await repository.list_runs("thread-a")) == 1
    assert len(await repository.list_messages("thread-a")) == 1


@pytest.mark.unit
async def test_different_threads_can_have_active_runs(repository) -> None:
    first = await repository.submit("thread-a", "first", "request-a")
    second = await repository.submit("thread-b", "second", "request-b")

    assert first.run.thread_id == "thread-a"
    assert second.run.thread_id == "thread-b"
    assert first.run.id != second.run.id


@pytest.mark.unit
async def test_concurrent_submissions_leave_one_active_run_per_thread(
    repository,
) -> None:
    results = await asyncio.gather(
        repository.submit("thread-a", "first", "request-a"),
        repository.submit("thread-a", "second", "request-b"),
        return_exceptions=True,
    )

    assert sum(not isinstance(result, Exception) for result in results) == 1
    assert sum(isinstance(result, ActiveRunConflictError) for result in results) == 1
    assert len(await repository.list_runs("thread-a")) == 1


@pytest.mark.unit
async def test_threads_can_be_listed_and_loaded(repository) -> None:
    await repository.submit("thread-a", "first", "request-a")
    await repository.submit("thread-b", "second", "request-b")

    threads = await repository.list_threads()
    loaded = await repository.get_thread("thread-a")

    assert {thread.id for thread in threads} == {"thread-a", "thread-b"}
    assert loaded.id == "thread-a"
    assert loaded.status == "active"
    assert loaded.created_at
    assert loaded.updated_at


@pytest.mark.unit
async def test_loading_missing_thread_raises_not_found(repository) -> None:
    with pytest.raises(RecordNotFoundError, match="thread not found"):
        await repository.get_thread("missing")
