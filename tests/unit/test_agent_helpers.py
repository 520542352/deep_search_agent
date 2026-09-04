from pathlib import Path

import pytest
from langchain_core.messages import AIMessage

from agent import main_agent


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
