from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from api import server


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(server, "output_dir", tmp_path / "output")
    monkeypatch.setattr(server, "upload_dir", tmp_path / "upload")
    server.output_dir.mkdir()
    server.upload_dir.mkdir()
    with TestClient(server.app) as test_client:
        yield test_client


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
    monkeypatch.setattr(server, "run_deep_agent", run_agent)

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
    assert response.json() == {"status": "started", "thread_id": "thread-a"}
    run_agent.assert_called_once_with("research", "thread-a")
    assert not server._background_tasks


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
def test_websocket_replies_to_ping(client: TestClient) -> None:
    with client.websocket_connect("/ws/thread-a") as websocket:
        websocket.send_text("ping")
        assert websocket.receive_json() == {
            "type": "pong",
            "message": "服务端已收到：ping",
        }
    assert "thread-a" not in server.manager.active_connections


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
