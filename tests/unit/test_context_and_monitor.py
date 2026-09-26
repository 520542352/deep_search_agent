import asyncio
from typing import Any

import pytest
from types import SimpleNamespace

from api import monitor as monitor_module
from api.context import (
    get_session_context,
    get_run_context,
    get_thread_context,
    reset_session_context,
    reset_run_context,
    reset_thread_context,
    set_run_context,
    set_session_context,
    set_thread_context,
)
from api.monitor import ConnectionManager, ToolMonitor


@pytest.mark.unit
def test_context_can_be_set_and_reset() -> None:
    session_token = set_session_context("session-a")
    thread_token = set_thread_context("thread-a")
    run_token = set_run_context("run-a")

    assert get_session_context() == "session-a"
    assert get_thread_context() == "thread-a"
    assert get_run_context() == "run-a"

    reset_session_context(session_token, thread_token)
    reset_run_context(run_token)
    assert get_session_context() is None
    assert get_thread_context() is None
    assert get_run_context() is None


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
async def test_connection_manager_broadcasts_to_every_connection_for_thread() -> None:
    manager = ConnectionManager()
    first = _FakeWebSocket()
    second = _FakeWebSocket()

    await manager.connect(first, "thread-a")
    await manager.connect(second, "thread-a")
    await manager.send_to_thread({"event": "done"}, "thread-a")
    await manager.send_to_thread({"event": "ignored"}, "thread-b")

    assert first.accepted is True
    assert second.accepted is True
    assert first.json_messages == [{"event": "done"}]
    assert second.json_messages == [{"event": "done"}]

    manager.disconnect(first, "thread-a")
    assert manager.active_connections["thread-a"] == {second}
    manager.disconnect(second, "thread-a")
    assert "thread-a" not in manager.active_connections


@pytest.mark.unit
async def test_monitor_schedules_send_on_same_event_loop() -> None:
    calls: list[dict[str, Any]] = []

    class _Publisher:
        async def publish(self, **kwargs) -> None:
            calls.append(kwargs)

    emitter = ToolMonitor()
    emitter.set_event_publisher(_Publisher(), asyncio.get_running_loop())
    thread_token = set_thread_context("thread-a")
    run_token = set_run_context("run-a")
    try:
        emitter._emit("tool_start", "working", {"step": 1})
        await asyncio.sleep(0)
    finally:
        emitter.set_event_publisher(None)
        reset_thread_context(thread_token)
        reset_run_context(run_token)

    assert calls == [{
        "thread_id": "thread-a",
        "run_id": "run-a",
        "event_type": "tool_start",
        "message": "working",
        "data": {"step": 1},
    }]


@pytest.mark.unit
def test_monitor_uses_threadsafe_publish_outside_manager_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, object]] = []
    manager_loop = object()

    class _Publisher:
        async def publish(self, **_kwargs) -> None:
            pytest.fail("the coroutine is scheduled onto the owning loop")

    def run_threadsafe(coroutine, loop):
        coroutine.close()
        calls.append(("scheduled", loop))

    monkeypatch.setattr(monitor_module.asyncio, "run_coroutine_threadsafe", run_threadsafe)
    emitter = ToolMonitor()
    emitter.set_event_publisher(_Publisher(), manager_loop)
    thread_token = set_thread_context("thread-a")
    try:
        emitter._emit("task_result", "done")
    finally:
        emitter.set_event_publisher(None)
        reset_thread_context(thread_token)

    assert calls == [("scheduled", manager_loop)]


@pytest.mark.unit
def test_monitor_skips_persistence_without_loop() -> None:
    class _Publisher:
        async def publish(self, **_kwargs):
            pytest.fail("publish must not run without an event loop")

    emitter = ToolMonitor()
    emitter.set_event_publisher(_Publisher(), None)
    try:
        emitter._emit("tool_start", "no loop")
    finally:
        emitter.set_event_publisher(None)


@pytest.mark.unit
def test_monitor_skips_persistence_without_thread_context() -> None:
    class _Publisher:
        async def publish(self, **_kwargs):
            pytest.fail("publish must not run without a thread context")

    emitter = ToolMonitor()
    emitter.set_event_publisher(_Publisher(), object())
    try:
        emitter._emit("tool_start", "no thread")
    finally:
        emitter.set_event_publisher(None)


@pytest.mark.unit
async def test_monitor_contains_event_publisher_failure() -> None:
    class _Publisher:
        async def publish(self, **_kwargs):
            raise RuntimeError("database failed")

    emitter = ToolMonitor()
    emitter.set_event_publisher(_Publisher(), asyncio.get_running_loop())
    thread_token = set_thread_context("thread-a")
    try:
        emitter._emit("tool_start", "still safe")
        await asyncio.sleep(0)
    finally:
        emitter.set_event_publisher(None)
        reset_thread_context(thread_token)


@pytest.mark.unit
async def test_monitor_drain_waits_for_scheduled_persistence() -> None:
    publish_started = asyncio.Event()
    release_publish = asyncio.Event()

    class _Publisher:
        async def publish(self, **_kwargs) -> None:
            publish_started.set()
            await release_publish.wait()

    emitter = ToolMonitor()
    emitter.set_event_publisher(_Publisher(), asyncio.get_running_loop())
    thread_token = set_thread_context("thread-a")
    try:
        emitter._emit("task_result", "done")
        await publish_started.wait()
        drain = asyncio.create_task(emitter.drain())
        await asyncio.sleep(0)
        assert drain.done() is False
        release_publish.set()
        await drain
    finally:
        emitter.set_event_publisher(None)
        reset_thread_context(thread_token)


@pytest.mark.unit
def test_monitor_writes_to_runtime_and_ignores_writer_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payloads: list[dict] = []
    emitter = ToolMonitor()
    emitter.set_event_publisher(None)
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


@pytest.mark.unit
async def test_connection_manager_removes_failed_socket_and_continues_broadcast() -> None:
    manager = ConnectionManager()
    healthy = _FakeWebSocket()
    failed = _FakeWebSocket()

    async def fail_send(_message: dict[str, Any]) -> None:
        raise RuntimeError("disconnected")

    failed.send_json = fail_send
    await manager.connect(healthy, "thread-a")
    await manager.connect(failed, "thread-a")

    await manager.send_to_thread({"event": "done"}, "thread-a")

    assert healthy.json_messages == [{"event": "done"}]
    assert manager.active_connections["thread-a"] == {healthy}
