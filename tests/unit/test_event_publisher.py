from pathlib import Path

import pytest

from api.events import EventPublisher
from persistence.database import Database, DatabaseNotConnectedError
from persistence.events import EventRepository
from persistence.repositories import ConversationRepository


@pytest.mark.unit
async def test_publisher_persists_before_broadcasting(tmp_path: Path) -> None:
    database = Database(tmp_path / "application.db")
    await database.connect()
    conversations = ConversationRepository(database)
    repository = EventRepository(database)
    submission = await conversations.submit("thread-a", "research", "request-a")

    class _Manager:
        async def send_to_thread(self, payload: dict, thread_id: str) -> None:
            persisted = await repository.list_after(thread_id, after_event_id=0)
            assert [event.id for event in persisted] == [payload["event_id"]]

    publisher = EventPublisher(repository, _Manager())
    try:
        event = await publisher.publish(
            thread_id="thread-a",
            run_id=submission.run.id,
            event_type="task_result",
            message="done",
            data={"result": "answer"},
        )

        assert event.event_type == "task_result"
    finally:
        await database.close()


@pytest.mark.unit
async def test_publisher_does_not_broadcast_when_persistence_fails(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "application.db")
    await database.connect()
    repository = EventRepository(database)
    await database.close()

    class _Manager:
        async def send_to_thread(self, *_args) -> None:
            pytest.fail("an unpersisted event must not be broadcast")

    publisher = EventPublisher(repository, _Manager())

    with pytest.raises(DatabaseNotConnectedError):
        await publisher.publish("thread-a", "error", "failed")
