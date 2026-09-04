import asyncio
from typing import Any

import pytest

from api.context import (
    get_session_context,
    get_thread_context,
    reset_session_context,
    set_session_context,
    set_thread_context,
)
from api.monitor import ConnectionManager


@pytest.mark.unit
def test_context_can_be_set_and_reset() -> None:
    session_token = set_session_context("session-a")
    thread_token = set_thread_context("thread-a")

    assert get_session_context() == "session-a"
    assert get_thread_context() == "thread-a"

    reset_session_context(session_token, thread_token)
    assert get_session_context() is None
    assert get_thread_context() is None


@pytest.mark.unit
async def test_context_is_isolated_between_async_tasks() -> None:
    async def worker(name: str) -> tuple[str | None, str | None]:
        session_token = set_session_context(f"session-{name}")
        thread_token = set_thread_context(f"thread-{name}")
        await asyncio.sleep(0)
        result = (get_session_context(), get_thread_context())
        reset_session_context(session_token, thread_token)
        return result

    first, second = await asyncio.gather(worker("a"), worker("b"))

    assert first == ("session-a", "thread-a")
    assert second == ("session-b", "thread-b")


class _FakeWebSocket:
    def __init__(self) -> None:
        self.accepted = False
        self.json_messages: list[dict[str, Any]] = []

    async def accept(self) -> None:
        self.accepted = True

    async def send_json(self, message: dict[str, Any]) -> None:
        self.json_messages.append(message)


@pytest.mark.unit
async def test_connection_manager_routes_message_to_thread() -> None:
    manager = ConnectionManager()
    websocket = _FakeWebSocket()

    await manager.connect(websocket, "thread-a")
    await manager.send_to_thread({"event": "done"}, "thread-a")
    await manager.send_to_thread({"event": "ignored"}, "thread-b")

    assert websocket.accepted is True
    assert websocket.json_messages == [{"event": "done"}]

    manager.disconnect(websocket, "thread-a")
    assert "thread-a" not in manager.active_connections
