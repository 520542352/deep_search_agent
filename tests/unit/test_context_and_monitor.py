import asyncio
from typing import Any

import pytest
from types import SimpleNamespace

from api import monitor as monitor_module
from api.context import (
    get_session_context,
    get_thread_context,
    reset_session_context,
    set_session_context,
    set_thread_context,
)
from api.monitor import ConnectionManager, ToolMonitor


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


@pytest.mark.unit
async def test_monitor_schedules_send_on_same_event_loop() -> None:
    payloads: list[tuple[dict, str]] = []

    class _Manager:
        def get_loop(self):
            return asyncio.get_running_loop()

        async def send_to_thread(self, payload: dict, thread_id: str) -> None:
            payloads.append((payload, thread_id))

    emitter = ToolMonitor()
    emitter.set_websocket_manager(_Manager())
    thread_token = set_thread_context("thread-a")
    try:
        emitter._emit("tool_start", "working", {"step": 1})
        await asyncio.sleep(0)
    finally:
        emitter.set_websocket_manager(None)
        reset_session_context(set_session_context(None), thread_token)

    assert len(payloads) == 1
    payload, thread_id = payloads[0]
    assert thread_id == "thread-a"
    assert payload["event"] == "tool_start"
    assert payload["message"] == "working"
    assert payload["data"] == {"step": 1}


@pytest.mark.unit
def test_monitor_uses_threadsafe_send_outside_manager_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[dict, str, object]] = []
    manager_loop = object()

    class _Manager:
        def get_loop(self):
            return manager_loop

        async def send_to_thread(self, payload: dict, thread_id: str) -> None:
            calls.append((payload, thread_id, manager_loop))

    def run_threadsafe(coroutine, loop):
        coroutine.close()
        calls.append(({}, "scheduled", loop))

    monkeypatch.setattr(monitor_module.asyncio, "run_coroutine_threadsafe", run_threadsafe)
    emitter = ToolMonitor()
    emitter.set_websocket_manager(_Manager())
    thread_token = set_thread_context("thread-a")
    try:
        emitter._emit("task_result", "done")
    finally:
        emitter.set_websocket_manager(None)
        reset_session_context(set_session_context(None), thread_token)

    assert calls == [({}, "scheduled", manager_loop)]


@pytest.mark.unit
def test_monitor_skips_websocket_without_loop_or_thread() -> None:
    class _Manager:
        def get_loop(self):
            return None

        async def send_to_thread(self, *_args):
            pytest.fail("send must not run without an event loop")

    emitter = ToolMonitor()
    emitter.set_websocket_manager(_Manager())
    try:
        emitter._emit("tool_start", "no loop")
    finally:
        emitter.set_websocket_manager(None)


@pytest.mark.unit
def test_monitor_skips_websocket_without_thread_context() -> None:
    class _Manager:
        def get_loop(self):
            return object()

        async def send_to_thread(self, *_args):
            pytest.fail("send must not run without a thread context")

    emitter = ToolMonitor()
    emitter.set_websocket_manager(_Manager())
    try:
        emitter._emit("tool_start", "no thread")
    finally:
        emitter.set_websocket_manager(None)


@pytest.mark.unit
def test_monitor_contains_websocket_manager_failure() -> None:
    class _Manager:
        def get_loop(self):
            raise RuntimeError("manager failed")

    emitter = ToolMonitor()
    emitter.set_websocket_manager(_Manager())
    try:
        emitter._emit("tool_start", "still safe")
    finally:
        emitter.set_websocket_manager(None)


@pytest.mark.unit
def test_monitor_writes_to_runtime_and_ignores_writer_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payloads: list[dict] = []
    emitter = ToolMonitor()
    emitter.set_websocket_manager(None)
    monkeypatch.setattr(
        monitor_module,
        "builtins",
        SimpleNamespace(runtime=SimpleNamespace(stream_writer=payloads.append)),
    )

    emitter._emit("session_created", "created")

    assert payloads[0]["event"] == "session_created"

    def fail_writer(_payload):
        raise RuntimeError("writer failed")

    monkeypatch.setattr(
        monitor_module,
        "builtins",
        SimpleNamespace(runtime=SimpleNamespace(stream_writer=fail_writer)),
    )
    emitter._emit("session_created", "still safe")


@pytest.mark.unit
def test_connection_manager_get_loop_is_safe_without_running_loop() -> None:
    manager = ConnectionManager()

    assert manager.get_loop() is None


@pytest.mark.unit
async def test_connection_manager_sends_personal_text_message() -> None:
    websocket = _FakeWebSocket()
    websocket.text_messages = []

    async def send_text(message: str) -> None:
        websocket.text_messages.append(message)

    websocket.send_text = send_text
    manager = ConnectionManager()

    await manager.send_personal_message("hello", websocket)

    assert websocket.text_messages == ["hello"]
