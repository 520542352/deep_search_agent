import asyncio
from pathlib import Path

import pytest

from persistence.database import Database
from persistence.repositories import (
    ActiveRunConflictError,
    ConversationRepository,
    InvalidRunTransitionError,
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


@pytest.mark.unit
async def test_startup_reconciliation_requeues_queued_and_interrupts_running(
    repository,
) -> None:
    queued = await repository.submit("thread-queued", "queued", "request-q")
    running = await repository.submit("thread-running", "running", "request-r")
    complete = await repository.submit("thread-complete", "complete", "request-c")
    await repository.mark_running(running.run.id)
    await repository.mark_running(complete.run.id)
    await repository.mark_succeeded(complete.run.id, "done")

    result = await repository.reconcile_startup()

    assert result.queued_run_ids == (queued.run.id,)
    assert result.interrupted_run_ids == (running.run.id,)
    interrupted = await repository.get_run(running.run.id)
    assert interrupted.status == "interrupted"
    assert interrupted.error_code == "ProcessInterrupted"
    assert interrupted.finished_at is not None
    assert (await repository.get_run(complete.run.id)).status == "succeeded"


@pytest.mark.unit
async def test_interrupted_run_can_be_claimed_once_for_resume(repository) -> None:
    submission = await repository.submit("thread-a", "research", "request-a")
    await repository.mark_running(submission.run.id)
    await repository.reconcile_startup()

    resumed = await repository.claim_resume(submission.run.id)

    assert resumed.status == "running"
    assert resumed.error_code is None
    assert resumed.error_message is None
    assert resumed.finished_at is None
    with pytest.raises(InvalidRunTransitionError, match="not interrupted"):
        await repository.claim_resume(submission.run.id)


@pytest.mark.unit
async def test_resume_rejects_thread_with_another_active_run(repository) -> None:
    interrupted = await repository.submit("thread-a", "first", "request-a")
    await repository.mark_running(interrupted.run.id)
    await repository.reconcile_startup()
    await repository.submit("thread-a", "second", "request-b")

    with pytest.raises(ActiveRunConflictError):
        await repository.claim_resume(interrupted.run.id)


@pytest.mark.unit
async def test_retry_creates_child_run_and_repeats_original_query(repository) -> None:
    original = await repository.submit(
        "thread-a",
        "research",
        "request-a",
        base_checkpoint_id="checkpoint-before-run",
    )
    await repository.mark_running(original.run.id)
    await repository.mark_failed(original.run.id, RuntimeError("model unavailable"))

    retry = await repository.retry_run(original.run.id, request_id="request-b")

    assert retry.parent_run_id == original.run.id
    assert retry.thread_id == original.run.thread_id
    assert retry.query == "research"
    assert retry.request_id == "request-b"
    assert retry.base_checkpoint_id == "checkpoint-before-run"
    assert retry.status == "queued"
    messages = await repository.list_messages("thread-a")
    assert [(message.role, message.content, message.run_id) for message in messages] == [
        ("user", "research", original.run.id),
        ("user", "research", retry.id),
    ]


@pytest.mark.unit
async def test_retry_rejects_non_recoverable_or_active_run(repository) -> None:
    queued = await repository.submit(
        "thread-a",
        "research",
        "request-a",
        base_checkpoint_id="checkpoint-before-run",
    )
    with pytest.raises(InvalidRunTransitionError, match="cannot be retried"):
        await repository.retry_run(queued.run.id, request_id="request-b")

    await repository.mark_running(queued.run.id)
    await repository.mark_failed(queued.run.id, RuntimeError("failed"))
    await repository.submit("thread-a", "new work", "request-c")
    with pytest.raises(ActiveRunConflictError):
        await repository.retry_run(queued.run.id, request_id="request-d")


@pytest.mark.unit
async def test_retry_rejects_legacy_run_without_checkpoint_baseline(repository) -> None:
    original = await repository.submit("thread-a", "research", "request-a")
    await repository.mark_running(original.run.id)
    await repository.mark_failed(original.run.id, RuntimeError("failed"))

    with pytest.raises(InvalidRunTransitionError, match="checkpoint baseline"):
        await repository.retry_run(original.run.id, request_id="request-b")
