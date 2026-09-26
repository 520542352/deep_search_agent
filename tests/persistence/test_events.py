from pathlib import Path

import pytest

from persistence.database import Database
from persistence.events import EventRepository
from persistence.repositories import ConversationRepository


@pytest.mark.unit
async def test_events_have_stable_ids_and_can_be_replayed_after_cursor(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "application.db")
    await database.connect()
    conversations = ConversationRepository(database)
    events = EventRepository(database)
    submission = await conversations.submit("thread-a", "research", "request-a")
    try:
        first = await events.append(
            thread_id="thread-a",
            run_id=submission.run.id,
            event_type="session_created",
            message="created",
            data={"path": "output/session_thread-a"},
        )
        second = await events.append(
            thread_id="thread-a",
            run_id=submission.run.id,
            event_type="tool_start",
            message="searching",
            data={"tool_name": "search"},
        )

        replay = await events.list_after("thread-a", after_event_id=first.id)

        assert second.id > first.id
        assert replay == [second]
        assert second.to_payload() == {
            "type": "monitor_event",
            "event_id": second.id,
            "run_id": submission.run.id,
            "event": "tool_start",
            "message": "searching",
            "data": {"tool_name": "search"},
            "timestamp": second.created_at,
        }
    finally:
        await database.close()


@pytest.mark.unit
async def test_event_replay_is_isolated_by_thread(tmp_path: Path) -> None:
    database = Database(tmp_path / "application.db")
    await database.connect()
    conversations = ConversationRepository(database)
    events = EventRepository(database)
    first = await conversations.submit("thread-a", "first", "request-a")
    second = await conversations.submit("thread-b", "second", "request-b")
    try:
        await events.append("thread-a", "tool_start", "a", run_id=first.run.id)
        await events.append("thread-b", "tool_start", "b", run_id=second.run.id)

        replay = await events.list_after("thread-a", after_event_id=0)

        assert [event.message for event in replay] == ["a"]
    finally:
        await database.close()
