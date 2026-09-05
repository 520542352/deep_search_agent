from types import SimpleNamespace

import pytest

from tools import rag_tools


pytestmark = pytest.mark.contract


class _FakeSession:
    def __init__(self, parts=(), error: Exception | None = None) -> None:
        self.id = "session-1"
        self.parts = parts
        self.error = error
        self.ask_calls: list[tuple[str, bool]] = []

    def ask(self, question: str, stream: bool):
        self.ask_calls.append((question, stream))
        if self.error:
            raise self.error
        return iter(self.parts)


class _FakeAssistant:
    def __init__(self, session: _FakeSession | None = None) -> None:
        self.name = "知识助手"
        self.description = "回答项目问题"
        self.datasets = [{"name": "项目文档"}, {"invalid": "ignored"}, "bad"]
        self.session = session or _FakeSession()
        self.created_names: list[str] = []
        self.deleted_ids: list[list[str]] = []
        self.delete_error: Exception | None = None

    def create_session(self, name: str):
        self.created_names.append(name)
        return self.session

    def delete_sessions(self, ids: list[str]) -> None:
        self.deleted_ids.append(ids)
        if self.delete_error:
            raise self.delete_error


def _set_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rag_tools, "_load_ragflow_env", lambda: ("key", "http://rag"))
    monkeypatch.setattr(rag_tools.monitor, "report_tool", lambda *args, **kwargs: None)


def test_get_assistant_list_formats_datasets(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_config(monkeypatch)
    assistant = _FakeAssistant()
    monkeypatch.setattr(
        rag_tools,
        "RAGFlow",
        lambda **kwargs: SimpleNamespace(list_chats=lambda: [assistant]),
    )

    result = rag_tools.get_assistant_list.invoke({})

    assert "助手名称:知识助手" in result
    assert "功能介绍:回答项目问题" in result
    assert "关联知识库:项目文档" in result


def test_get_assistant_list_handles_empty_and_sdk_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_config(monkeypatch)
    monkeypatch.setattr(
        rag_tools,
        "RAGFlow",
        lambda **kwargs: SimpleNamespace(list_chats=lambda: []),
    )
    assert rag_tools.get_assistant_list.invoke({}) == "未找到聊天助手"

    def fail_list():
        raise RuntimeError("list failed")

    monkeypatch.setattr(
        rag_tools,
        "RAGFlow",
        lambda **kwargs: SimpleNamespace(list_chats=fail_list),
    )
    assert "list failed" in rag_tools.get_assistant_list.invoke({})


@pytest.mark.parametrize("tool", [rag_tools.get_assistant_list, rag_tools.create_ask_delete])
def test_ragflow_tools_reject_missing_configuration(
    monkeypatch: pytest.MonkeyPatch,
    tool,
) -> None:
    monkeypatch.setattr(rag_tools, "_load_ragflow_env", lambda: (None, None))
    monkeypatch.setattr(rag_tools.monitor, "report_tool", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        rag_tools,
        "RAGFlow",
        lambda **_kwargs: pytest.fail("SDK must not be initialized without configuration"),
    )
    arguments = {} if tool is rag_tools.get_assistant_list else {
        "assistant_name": "知识助手",
        "question": "问题",
    }

    result = tool.invoke(arguments)

    assert "未成功配置" in result


def test_ask_streams_last_answer_and_always_deletes_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_config(monkeypatch)
    session = _FakeSession(
        [SimpleNamespace(content="第一段"), SimpleNamespace(content="完整答案")]
    )
    assistant = _FakeAssistant(session)
    list_calls: list[dict] = []

    def list_chats(**kwargs):
        list_calls.append(kwargs)
        return [assistant]

    monkeypatch.setattr(
        rag_tools, "RAGFlow", lambda **kwargs: SimpleNamespace(list_chats=list_chats)
    )

    result = rag_tools.create_ask_delete.invoke(
        {"assistant_name": "知识助手", "question": "怎么测试？"}
    )

    assert result == "完整答案"
    assert list_calls == [{"name": "知识助手"}]
    assert assistant.created_names == ["temp_session_for_single_ask"]
    assert session.ask_calls == [("怎么测试？", True)]
    assert assistant.deleted_ids == [["session-1"]]


def test_ask_reports_empty_answer_and_missing_assistant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_config(monkeypatch)
    assistant = _FakeAssistant(_FakeSession([SimpleNamespace(content="")]))
    monkeypatch.setattr(
        rag_tools,
        "RAGFlow",
        lambda **kwargs: SimpleNamespace(list_chats=lambda **_kwargs: [assistant]),
    )
    result = rag_tools.create_ask_delete.invoke(
        {"assistant_name": "知识助手", "question": "空答案"}
    )
    assert result == "未获取到助手的回答"
    assert assistant.deleted_ids == [["session-1"]]

    monkeypatch.setattr(
        rag_tools,
        "RAGFlow",
        lambda **kwargs: SimpleNamespace(list_chats=lambda **_kwargs: []),
    )
    result = rag_tools.create_ask_delete.invoke(
        {"assistant_name": "不存在", "question": "问题"}
    )
    assert "未找到" in result


def test_ask_deletes_session_when_streaming_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_config(monkeypatch)
    assistant = _FakeAssistant(_FakeSession(error=RuntimeError("stream failed")))
    monkeypatch.setattr(
        rag_tools,
        "RAGFlow",
        lambda **kwargs: SimpleNamespace(list_chats=lambda **_kwargs: [assistant]),
    )

    result = rag_tools.create_ask_delete.invoke(
        {"assistant_name": "知识助手", "question": "失败问题"}
    )

    assert "stream failed" in result
    assert assistant.deleted_ids == [["session-1"]]


def test_ask_does_not_lose_answer_when_cleanup_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_config(monkeypatch)
    assistant = _FakeAssistant(_FakeSession([SimpleNamespace(content="答案")]))
    assistant.delete_error = RuntimeError("delete failed")
    monkeypatch.setattr(
        rag_tools,
        "RAGFlow",
        lambda **kwargs: SimpleNamespace(list_chats=lambda **_kwargs: [assistant]),
    )

    result = rag_tools.create_ask_delete.invoke(
        {"assistant_name": "知识助手", "question": "问题"}
    )

    assert result == "答案"
    assert assistant.deleted_ids == [["session-1"]]


def test_ask_converts_ragflow_initialization_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_config(monkeypatch)

    def fail_init(**_kwargs):
        raise RuntimeError("init failed")

    monkeypatch.setattr(rag_tools, "RAGFlow", fail_init)

    result = rag_tools.create_ask_delete.invoke(
        {"assistant_name": "知识助手", "question": "问题"}
    )

    assert "RAGFlow 操作失败" in result
    assert "init failed" in result

