import asyncio
from pathlib import Path
from functools import partial
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from api import server
from persistence.database import Database
from persistence.repositories import ConversationRepository


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(server, "output_dir", tmp_path / "output")
    monkeypatch.setattr(server, "upload_dir", tmp_path / "upload")
    monkeypatch.setattr(server, "database_path", tmp_path / "data" / "application.db")
    monkeypatch.setattr(server, "checkpoint_path", tmp_path / "data" / "checkpoints.db")
    server.output_dir.mkdir()
    server.upload_dir.mkdir()
    with TestClient(server.app) as test_client:
        yield test_client


@pytest.mark.api
def test_application_lifespan_initializes_and_closes_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_path = tmp_path / "data" / "application.db"
    checkpoint_path = tmp_path / "data" / "checkpoints.db"
    monkeypatch.setattr(server, "database_path", database_path)
    monkeypatch.setattr(server, "checkpoint_path", checkpoint_path)

    with TestClient(server.app):
        database = server.app.state.database
        agent_runtime = server.app.state.agent_runtime
        repository = server.app.state.conversation_repository
        agent_runner = server.app.state.agent_runner
        event_repository = server.app.state.event_repository
        event_publisher = server.app.state.event_publisher
        artifact_repository = server.app.state.artifact_repository
        artifact_service = server.app.state.artifact_service
        assert database.path == database_path
        assert database.is_connected is True
        assert agent_runtime.path == checkpoint_path
        assert agent_runtime.is_started is True
        assert repository.database is database
        assert agent_runner.repository is repository
        assert agent_runner.agent is agent_runtime.agent
        assert event_repository.database is database
        assert event_publisher.repository is event_repository
        assert artifact_repository.database is database
        assert artifact_service.repository is artifact_repository
        assert agent_runner.artifact_service is artifact_service
        assert checkpoint_path.is_file()

    assert database.is_connected is False
    assert agent_runtime.is_started is False


@pytest.mark.api
def test_task_rejects_invalid_thread_id(client: TestClient) -> None:
    response = client.post(
        "/api/task", json={"query": "research", "thread_id": "../escape"}
    )

    assert response.status_code == 400
    assert "thread_id" in response.json()["detail"]


@pytest.mark.api
def test_task_schedules_agent(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    run_agent = AsyncMock()
    client.app.state.agent_runner.run = run_agent
    ensure_baseline = AsyncMock(return_value="checkpoint-before-run")
    client.app.state.agent_runtime.ensure_checkpoint_baseline = ensure_baseline

    class _Task:
        def add_done_callback(self, callback):
            callback(self)

        def exception(self):
            return None

    def close_coroutine(coroutine):
        coroutine.close()
        return _Task()

    monkeypatch.setattr(server.asyncio, "create_task", close_coroutine)

    response = client.post(
        "/api/task",
        json={
            "query": "research",
            "thread_id": "thread-a",
            "request_id": "request-a",
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body == {
        "status": "started",
        "thread_id": "thread-a",
        "run_id": body["run_id"],
        "request_id": "request-a",
        "deduplicated": False,
    }
    run_agent.assert_called_once_with(body["run_id"])
    persisted = client.portal.call(
        client.app.state.conversation_repository.get_run,
        body["run_id"],
    )
    assert persisted.base_checkpoint_id == "checkpoint-before-run"
    ensure_baseline.assert_awaited_once_with("thread-a")
    assert not server._background_tasks


@pytest.mark.api
def test_task_persists_original_query_and_deduplicates_request(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scheduled: list[object] = []

    class _Task:
        def add_done_callback(self, callback):
            callback(self)

        def exception(self):
            return None

    def close_coroutine(coroutine):
        scheduled.append(coroutine)
        coroutine.close()
        return _Task()

    monkeypatch.setattr(server.asyncio, "create_task", close_coroutine)

    payload = {
        "query": "original question",
        "thread_id": "thread-a",
        "request_id": "request-a",
    }
    first = client.post("/api/task", json=payload)
    duplicate = client.post("/api/task", json=payload)

    assert first.status_code == 200
    assert duplicate.status_code == 200
    assert duplicate.json()["run_id"] == first.json()["run_id"]
    assert duplicate.json()["deduplicated"] is True
    assert len(scheduled) == 1


@pytest.mark.api
def test_task_rejects_second_active_run_for_thread(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Task:
        def add_done_callback(self, callback):
            callback(self)

        def exception(self):
            return None

    def close_coroutine(coroutine):
        coroutine.close()
        return _Task()

    monkeypatch.setattr(server.asyncio, "create_task", close_coroutine)
    first = client.post(
        "/api/task",
        json={"query": "first", "thread_id": "thread-a", "request_id": "request-a"},
    )
    conflict = client.post(
        "/api/task",
        json={"query": "second", "thread_id": "thread-a", "request_id": "request-b"},
    )

    assert first.status_code == 200
    assert conflict.status_code == 409
    assert conflict.json()["detail"] == "该会话已有正在执行的任务"


@pytest.mark.api
def test_task_generates_request_id_when_omitted(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Task:
        def add_done_callback(self, callback):
            callback(self)

        def exception(self):
            return None

    def close_coroutine(coroutine):
        coroutine.close()
        return _Task()

    monkeypatch.setattr(server.asyncio, "create_task", close_coroutine)

    response = client.post(
        "/api/task", json={"query": "research", "thread_id": "thread-a"}
    )

    assert response.status_code == 200
    assert response.json()["request_id"]


@pytest.mark.api
def test_task_rejects_missing_and_blank_query(client: TestClient) -> None:
    missing = client.post("/api/task", json={"thread_id": "thread-a"})
    blank = client.post("/api/task", json={"query": "   ", "thread_id": "thread-a"})

    assert missing.status_code == 422
    assert blank.status_code == 400
    assert blank.json()["detail"] == "query 不能为空"


@pytest.mark.api
def test_background_task_handler_discards_and_logs_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task = Mock()
    task.exception.return_value = RuntimeError("unexpected failure")
    server._background_tasks.add(task)
    errors: list[str] = []
    monkeypatch.setattr(server.logger, "error", errors.append)

    server._handle_background_task(task)

    assert task not in server._background_tasks
    assert errors == ["Agent background task failed: unexpected failure"]


@pytest.mark.api
def test_background_task_handler_accepts_cancellation() -> None:
    task = Mock()
    task.exception.side_effect = server.asyncio.CancelledError
    server._background_tasks.add(task)

    server._handle_background_task(task)

    assert task not in server._background_tasks


@pytest.mark.api
def test_upload_sanitizes_filename(client: TestClient, tmp_path: Path) -> None:
    response = client.post(
        "/api/upload",
        data={"thread_id": "thread-a"},
        files={"files": ("../note.txt", b"hello", "text/plain")},
    )

    assert response.status_code == 200
    assert response.json() == {"status": "uploaded", "files": ["note.txt"]}
    assert (
        tmp_path / "upload" / "session_thread-a" / "note.txt"
    ).read_bytes() == b"hello"


@pytest.mark.api
def test_upload_rejects_invalid_filename(client: TestClient) -> None:
    response = client.post(
        "/api/upload",
        data={"thread_id": "thread-a"},
        files={"files": ("..", b"bad", "application/octet-stream")},
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "无效的文件名"


@pytest.mark.api
def test_upload_rejects_invalid_thread_id(client: TestClient) -> None:
    response = client.post(
        "/api/upload",
        data={"thread_id": "bad!id"},
        files={"files": ("note.txt", b"hello", "text/plain")},
    )

    assert response.status_code == 400
    assert "thread_id" in response.json()["detail"]


@pytest.mark.api
def test_upload_reports_directory_creation_failure(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_mkdir = Path.mkdir

    def fail_target(self: Path, *args, **kwargs):
        if self.name == "session_thread-a" and self.parent == server.upload_dir:
            raise OSError("disk unavailable")
        return original_mkdir(self, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", fail_target)

    response = client.post(
        "/api/upload",
        data={"thread_id": "thread-a"},
        files={"files": ("note.txt", b"hello", "text/plain")},
    )

    assert response.status_code == 500
    assert response.json()["detail"] == "无法创建上传目录"


@pytest.mark.api
def test_upload_removes_partial_file_after_write_failure(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        server.shutil,
        "copyfileobj",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("disk full")),
    )

    response = client.post(
        "/api/upload",
        data={"thread_id": "thread-a"},
        files={"files": ("note.txt", b"hello", "text/plain")},
    )

    assert response.status_code == 500
    assert response.json()["detail"] == "保存上传文件失败"
    assert not (tmp_path / "upload" / "session_thread-a" / "note.txt").exists()


@pytest.mark.api
def test_upload_restores_existing_file_when_metadata_registration_fails(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    first = client.post(
        "/api/upload",
        data={"thread_id": "thread-a"},
        files={"files": ("note.txt", b"original", "text/plain")},
    )
    assert first.status_code == 200
    monkeypatch.setattr(
        client.app.state.artifact_service,
        "register_upload",
        AsyncMock(side_effect=RuntimeError("database unavailable")),
    )

    failed = client.post(
        "/api/upload",
        data={"thread_id": "thread-a"},
        files={"files": ("note.txt", b"replacement", "text/plain")},
    )

    assert failed.status_code == 500
    assert failed.json()["detail"] == "登记上传文件失败"
    assert (
        tmp_path / "upload" / "session_thread-a" / "note.txt"
    ).read_bytes() == b"original"


@pytest.mark.api
def test_list_and_download_files_are_confined_to_output(
    client: TestClient, tmp_path: Path
) -> None:
    session_dir = tmp_path / "output" / "session_thread-a"
    session_dir.mkdir()
    report = session_dir / "report.md"
    report.write_text("report", encoding="utf-8")
    outside = tmp_path / "secret.txt"
    outside.write_text("secret", encoding="utf-8")

    listed = client.get("/api/files", params={"path": str(session_dir)})
    downloaded = client.get("/api/download", params={"path": str(report)})
    denied = client.get("/api/download", params={"path": str(outside)})
    denied_list = client.get("/api/files", params={"path": str(tmp_path)})

    assert listed.status_code == 200
    assert [item["name"] for item in listed.json()["files"]] == ["report.md"]
    assert downloaded.content == b"report"
    assert denied.status_code == 403
    assert "拒绝访问" in denied.json()["detail"]
    assert denied_list.status_code == 403
    assert "拒绝访问" in denied_list.json()["detail"]


@pytest.mark.api
def test_file_endpoints_validate_required_and_missing_paths(
    client: TestClient,
    tmp_path: Path,
) -> None:
    assert client.get("/api/files").status_code == 422
    assert client.get("/api/download").status_code == 422

    missing_file = tmp_path / "output" / "missing.txt"
    missing_dir = tmp_path / "output" / "missing-dir"
    download = client.get("/api/download", params={"path": str(missing_file)})
    listing = client.get("/api/files", params={"path": str(missing_dir)})

    assert download.status_code == 404
    assert download.json()["detail"] == "文件不存在"
    assert listing.status_code == 404
    assert listing.json()["detail"] == "目录不存在"


@pytest.mark.api
def test_history_endpoints_return_persisted_records_without_server_paths(
    client: TestClient,
) -> None:
    conversations = client.app.state.conversation_repository
    events = client.app.state.event_repository
    submission = client.portal.call(
        conversations.submit,
        "thread-a",
        "research",
        "request-a",
    )
    client.portal.call(conversations.mark_running, submission.run.id)
    client.portal.call(
        conversations.mark_succeeded,
        submission.run.id,
        "final answer",
    )
    client.portal.call(
        partial(
            events.append,
            "thread-a",
            "tool_end",
            "finished",
            run_id=submission.run.id,
            data={"tool_name": "search"},
        )
    )
    upload = client.post(
        "/api/upload",
        data={"thread_id": "thread-a"},
        files={"files": ("note.txt", b"hello", "text/plain")},
    )
    assert upload.status_code == 200

    threads = client.get("/api/threads")
    thread = client.get("/api/threads/thread-a")
    messages = client.get("/api/threads/thread-a/messages")
    runs = client.get("/api/threads/thread-a/runs")
    run = client.get(f"/api/runs/{submission.run.id}")
    persisted_events = client.get("/api/threads/thread-a/events")
    artifacts = client.get("/api/threads/thread-a/artifacts")

    assert threads.status_code == 200
    assert [item["id"] for item in threads.json()] == ["thread-a"]
    assert thread.json()["status"] == "active"
    assert [(item["role"], item["content"]) for item in messages.json()] == [
        ("user", "research"),
        ("assistant", "final answer"),
    ]
    assert [item["id"] for item in runs.json()] == [submission.run.id]
    assert run.json()["status"] == "succeeded"
    assert persisted_events.json()[0]["event_type"] == "tool_end"
    assert persisted_events.json()[0]["data"] == {"tool_name": "search"}
    assert artifacts.json()[0]["filename"] == "note.txt"
    assert artifacts.json()[0]["kind"] == "upload"
    assert "relative_path" not in artifacts.json()[0]
    assert str(server.upload_dir) not in str(artifacts.json())


@pytest.mark.api
def test_artifact_download_uses_persisted_id(client: TestClient) -> None:
    uploaded = client.post(
        "/api/upload",
        data={"thread_id": "thread-a"},
        files={"files": ("note.txt", b"hello", "text/plain")},
    )
    assert uploaded.status_code == 200
    artifact = client.get("/api/threads/thread-a/artifacts").json()[0]

    downloaded = client.get(f"/api/artifacts/{artifact['id']}/download")

    assert downloaded.status_code == 200
    assert downloaded.content == b"hello"
    assert 'filename="note.txt"' in downloaded.headers["content-disposition"]


@pytest.mark.api
@pytest.mark.parametrize(
    "path",
    [
        "/api/threads/missing",
        "/api/threads/missing/messages",
        "/api/threads/missing/runs",
        "/api/threads/missing/events",
        "/api/threads/missing/artifacts",
        "/api/runs/missing",
        "/api/artifacts/missing/download",
    ],
)
def test_history_endpoints_return_404_for_missing_records(
    client: TestClient,
    path: str,
) -> None:
    response = client.get(path)

    assert response.status_code == 404


@pytest.mark.api
def test_legacy_path_endpoints_are_marked_deprecated(client: TestClient) -> None:
    schema = client.get("/openapi.json").json()

    assert schema["paths"]["/api/files"]["get"]["deprecated"] is True
    assert schema["paths"]["/api/download"]["get"]["deprecated"] is True


@pytest.mark.api
def test_lifespan_requeues_queued_runs_and_interrupts_stale_running(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = tmp_path / "data" / "application.db"
    checkpoint_path = tmp_path / "data" / "checkpoints.db"

    async def seed_runs():
        database = Database(database_path)
        await database.connect()
        repository = ConversationRepository(database)
        queued = await repository.submit("thread-queued", "queued", "request-q")
        running = await repository.submit("thread-running", "running", "request-r")
        await repository.mark_running(running.run.id)
        await database.close()
        return queued.run.id, running.run.id

    queued_run_id, running_run_id = asyncio.run(seed_runs())
    scheduled: list[str] = []

    async def record_run(_runner, run_id: str) -> None:
        scheduled.append(run_id)

    monkeypatch.setattr(server, "database_path", database_path)
    monkeypatch.setattr(server, "checkpoint_path", checkpoint_path)
    monkeypatch.setattr(server, "output_dir", tmp_path / "output")
    monkeypatch.setattr(server, "upload_dir", tmp_path / "upload")
    monkeypatch.setattr(server.AgentRunner, "run", record_run)
    server.output_dir.mkdir()
    server.upload_dir.mkdir()

    with TestClient(server.app) as test_client:
        test_client.get("/api/threads")
        interrupted = test_client.get(f"/api/runs/{running_run_id}").json()

    assert scheduled == [queued_run_id]
    assert interrupted["status"] == "interrupted"
    assert interrupted["error_code"] == "ProcessInterrupted"


@pytest.mark.api
def test_resume_claims_interrupted_run_before_scheduling(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = client.app.state.conversation_repository
    submission = client.portal.call(
        repository.submit,
        "thread-a",
        "research",
        "request-a",
    )
    client.portal.call(repository.mark_running, submission.run.id)
    client.portal.call(repository.reconcile_startup)
    monkeypatch.setattr(
        client.app.state.agent_runtime,
        "checkpoint_id_for_run",
        AsyncMock(return_value="checkpoint-for-run"),
    )
    resume_agent = AsyncMock()
    client.app.state.agent_runner.resume = resume_agent

    class _Task:
        def add_done_callback(self, callback):
            callback(self)

        def exception(self):
            return None

    def close_coroutine(coroutine):
        coroutine.close()
        return _Task()

    monkeypatch.setattr(server.asyncio, "create_task", close_coroutine)

    resumed = client.post(f"/api/runs/{submission.run.id}/resume")
    duplicate = client.post(f"/api/runs/{submission.run.id}/resume")

    assert resumed.status_code == 200
    assert resumed.json() == {
        "status": "resumed",
        "thread_id": "thread-a",
        "run_id": submission.run.id,
        "parent_run_id": None,
    }
    assert duplicate.status_code == 409
    assert client.portal.call(repository.get_run, submission.run.id).status == "running"
    resume_agent.assert_called_once_with(
        submission.run.id,
        checkpoint_id="checkpoint-for-run",
    )


@pytest.mark.api
def test_resume_without_checkpoint_keeps_run_interrupted(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = client.app.state.conversation_repository
    submission = client.portal.call(
        repository.submit,
        "thread-a",
        "research",
        "request-a",
    )
    client.portal.call(repository.mark_running, submission.run.id)
    client.portal.call(repository.reconcile_startup)
    monkeypatch.setattr(
        client.app.state.agent_runtime,
        "checkpoint_id_for_run",
        AsyncMock(return_value=None),
    )

    response = client.post(f"/api/runs/{submission.run.id}/resume")

    assert response.status_code == 409
    assert response.json()["detail"] == "该运行没有可恢复的检查点"
    assert client.portal.call(repository.get_run, submission.run.id).status == "interrupted"


@pytest.mark.api
def test_retry_creates_and_schedules_child_run(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = client.app.state.conversation_repository
    original = client.portal.call(
        partial(
            repository.submit,
            "thread-a",
            "research",
            "request-a",
            base_checkpoint_id="checkpoint-before-run",
        )
    )
    client.portal.call(repository.mark_running, original.run.id)
    client.portal.call(
        repository.mark_failed,
        original.run.id,
        RuntimeError("model unavailable"),
    )
    run_agent = AsyncMock()
    client.app.state.agent_runner.run = run_agent

    class _Task:
        def add_done_callback(self, callback):
            callback(self)

        def exception(self):
            return None

    def close_coroutine(coroutine):
        coroutine.close()
        return _Task()

    monkeypatch.setattr(server.asyncio, "create_task", close_coroutine)

    response = client.post(f"/api/runs/{original.run.id}/retry")

    assert response.status_code == 200
    body = response.json()
    assert body == {
        "status": "retried",
        "thread_id": "thread-a",
        "run_id": body["run_id"],
        "parent_run_id": original.run.id,
    }
    retry = client.portal.call(repository.get_run, body["run_id"])
    assert retry.status == "queued"
    assert retry.query == "research"
    run_agent.assert_called_once_with(retry.id)


@pytest.mark.api
def test_recovery_endpoints_reject_missing_or_terminal_runs(
    client: TestClient,
) -> None:
    missing_resume = client.post("/api/runs/missing/resume")
    missing_retry = client.post("/api/runs/missing/retry")
    repository = client.app.state.conversation_repository
    submission = client.portal.call(
        repository.submit,
        "thread-a",
        "research",
        "request-a",
    )
    client.portal.call(repository.mark_running, submission.run.id)
    client.portal.call(repository.mark_succeeded, submission.run.id, "done")

    completed_resume = client.post(f"/api/runs/{submission.run.id}/resume")
    completed_retry = client.post(f"/api/runs/{submission.run.id}/retry")

    assert missing_resume.status_code == 404
    assert missing_retry.status_code == 404
    assert completed_resume.status_code == 409
    assert completed_retry.status_code == 409


@pytest.mark.api
def test_websocket_replies_to_ping(client: TestClient) -> None:
    with client.websocket_connect("/ws/thread-a") as websocket:
        websocket.send_text("ping")
        assert websocket.receive_json() == {
            "type": "pong",
            "message": "服务端已收到：ping",
        }
    assert "thread-a" not in server.manager.active_connections


@pytest.mark.api
def test_websocket_replays_events_after_cursor(client: TestClient) -> None:
    conversations = client.app.state.conversation_repository
    events = client.app.state.event_repository
    submission = client.portal.call(
        conversations.submit,
        "thread-a",
        "research",
        "request-a",
    )
    first = client.portal.call(
        partial(
            events.append,
            "thread-a",
            "session_created",
            "created",
            run_id=submission.run.id,
        )
    )
    second = client.portal.call(
        partial(
            events.append,
            "thread-a",
            "tool_start",
            "searching",
            run_id=submission.run.id,
            data={"tool_name": "search"},
        )
    )

    with client.websocket_connect(
        f"/ws/thread-a?after_event_id={first.id}"
    ) as websocket:
        assert websocket.receive_json() == second.to_payload()
        websocket.send_text("ping")
        assert websocket.receive_json()["type"] == "pong"


@pytest.mark.api
def test_websocket_rejects_invalid_thread_id(client: TestClient) -> None:
    with pytest.raises(WebSocketDisconnect) as exc_info:
        with client.websocket_connect("/ws/bad!id"):
            pass

    assert exc_info.value.code == 1008


@pytest.mark.api
async def test_websocket_disconnects_after_receive_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    websocket = AsyncMock()
    websocket.receive_text.side_effect = RuntimeError("socket failed")
    connect = AsyncMock()
    disconnect = Mock()
    monkeypatch.setattr(server.manager, "connect", connect)
    monkeypatch.setattr(server.manager, "disconnect", disconnect)

    await server.websocket_endpoint(websocket, "thread-a")

    connect.assert_awaited_once_with(websocket, "thread-a")
    disconnect.assert_called_once_with(websocket, "thread-a")
