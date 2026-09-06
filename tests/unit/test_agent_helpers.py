from pathlib import Path

import pytest
from langchain_core.messages import AIMessage

from agent import main_agent
from api.context import get_session_context, get_thread_context


@pytest.mark.unit
def test_prepare_session_environment_copies_uploaded_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(main_agent, "project_root_path", tmp_path)
    upload_dir = tmp_path / "upload" / "session_thread-a"
    upload_dir.mkdir(parents=True)
    (upload_dir / "source.txt").write_text("content", encoding="utf-8")

    absolute, relative, upload_info = main_agent._prepare_session_environment("thread-a")

    assert Path(absolute) == (tmp_path / "output" / "session_thread-a").resolve()
    assert relative == "output/session_thread-a"
    assert "source.txt" in upload_info
    assert (tmp_path / relative / "source.txt").read_text(encoding="utf-8") == "content"


@pytest.mark.unit
def test_process_stream_chunk_reports_subagent_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        main_agent.monitor,
        "report_assistant",
        lambda name, args: calls.append((name, args)),
    )
    message = AIMessage(
        content="",
        tool_calls=[
            {
                "name": "task",
                "args": {"subagent_type": "search", "description": "research"},
                "id": "call-1",
            }
        ],
    )

    main_agent._process_stream_chunk({"model": {"messages": [message]}})

    assert calls == [("search", {"desc": "research"})]


@pytest.mark.unit
def test_process_stream_chunk_reports_final_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    results: list[str] = []
    monkeypatch.setattr(main_agent.monitor, "report_task_result", results.append)

    main_agent._process_stream_chunk(
        {"model": {"messages": [AIMessage(content="final answer")]}}
    )

    assert results == ["final answer"]


class _StreamingAgent:
    def __init__(self, chunks=(), error: Exception | None = None) -> None:
        self.chunks = chunks
        self.error = error
        self.calls: list[tuple[dict, dict]] = []

    async def astream(self, inputs: dict, config: dict):
        self.calls.append((inputs, config))
        for chunk in self.chunks:
            yield chunk
        if self.error:
            raise self.error


@pytest.mark.unit
async def test_run_agent_builds_request_consumes_stream_and_resets_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = _StreamingAgent(
        [{"model": {"messages": [AIMessage(content="final answer")]}}]
    )
    session_reports: list[str] = []
    result_reports: list[str] = []
    monkeypatch.setattr(main_agent, "main_agent", agent)
    monkeypatch.setattr(
        main_agent,
        "_prepare_session_environment",
        lambda thread_id: ("C:/sessions/thread-a", "output/session_thread-a", "uploaded"),
    )
    monkeypatch.setattr(main_agent.monitor, "report_session_dir", session_reports.append)
    monkeypatch.setattr(main_agent.monitor, "report_task_result", result_reports.append)

    result = await main_agent.run_deep_agent("research", "thread-a")

    assert result == "Done"
    assert session_reports == ["C:/sessions/thread-a"]
    assert result_reports == ["final answer"]
    inputs, config = agent.calls[0]
    assert config == {"configurable": {"thread_id": "thread-a"}}
    assert inputs["messages"][0]["content"].startswith("research")
    assert "output/session_thread-a" in inputs["messages"][0]["content"]
    assert "uploaded" in inputs["messages"][0]["content"]
    assert get_session_context() is None
    assert get_thread_context() is None


@pytest.mark.unit
async def test_run_agent_reports_stream_failure_and_resets_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = _StreamingAgent(error=RuntimeError("stream failed"))
    errors: list[tuple[str, str]] = []
    monkeypatch.setattr(main_agent, "main_agent", agent)
    monkeypatch.setattr(
        main_agent,
        "_prepare_session_environment",
        lambda thread_id: ("C:/sessions/thread-a", "output/session_thread-a", ""),
    )
    monkeypatch.setattr(main_agent.monitor, "report_session_dir", lambda _path: None)
    monkeypatch.setattr(
        main_agent.monitor, "_emit", lambda event, message: errors.append((event, message))
    )

    result = await main_agent.run_deep_agent("research", "thread-a")

    assert result == "Error: stream failed"
    assert errors == [("error", "Exception failed: stream failed")]
    assert get_session_context() is None
    assert get_thread_context() is None


@pytest.mark.unit
async def test_run_agent_converts_environment_preparation_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        main_agent,
        "_prepare_session_environment",
        lambda _thread_id: (_ for _ in ()).throw(ValueError("invalid workspace")),
    )
    errors: list[str] = []
    monkeypatch.setattr(
        main_agent.monitor, "_emit", lambda _event, message: errors.append(message)
    )

    result = await main_agent.run_deep_agent("research", "bad")

    assert result == "Error: invalid workspace"
    assert errors == ["Exception failed: invalid workspace"]
    assert get_session_context() is None
    assert get_thread_context() is None


@pytest.mark.unit
async def test_run_agent_accepts_stream_without_final_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = _StreamingAgent([{"tools": {"messages": []}}])
    monkeypatch.setattr(main_agent, "main_agent", agent)
    monkeypatch.setattr(
        main_agent,
        "_prepare_session_environment",
        lambda thread_id: ("C:/sessions/thread-a", "output/session_thread-a", ""),
    )
    monkeypatch.setattr(main_agent.monitor, "report_session_dir", lambda _path: None)

    assert await main_agent.run_deep_agent("research", "thread-a") == "Done"
