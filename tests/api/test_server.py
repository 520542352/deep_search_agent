from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

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
        pass

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

    assert listed.status_code == 200
    assert [item["name"] for item in listed.json()["files"]] == ["report.md"]
    assert downloaded.content == b"report"
    assert "拒绝访问" in denied.json()["error"]


@pytest.mark.api
def test_websocket_replies_to_ping(client: TestClient) -> None:
    with client.websocket_connect("/ws/thread-a") as websocket:
        websocket.send_text("ping")
        assert websocket.receive_json() == {
            "type": "pong",
            "message": "服务端已收到：ping",
        }
