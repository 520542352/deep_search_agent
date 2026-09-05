from types import SimpleNamespace

import pytest

from tools import tavily_tool


pytestmark = pytest.mark.contract


class _FakeTavilyClient:
    def __init__(self, result=None, error: Exception | None = None) -> None:
        self.result = result
        self.error = error
        self.calls: list[dict] = []

    def search(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return self.result


def test_search_forwards_arguments_and_reports_monitor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _FakeTavilyClient(
        {"query": "pytest", "results": [{"url": "https://example.test"}]}
    )
    reports: list[tuple[str, dict]] = []
    monkeypatch.setattr(tavily_tool, "_create_tavily_client", lambda: client)
    monkeypatch.setattr(
        tavily_tool,
        "monitor",
        SimpleNamespace(report_tool=lambda tool_name, args: reports.append((tool_name, args))),
    )

    result = tavily_tool.internet_search.invoke(
        {
            "query": "pytest",
            "max_results": 3,
            "topic": "news",
            "include_raw_content": True,
        }
    )

    assert result == client.result
    assert client.calls == [
        {
            "query": "pytest",
            "topic": "news",
            "max_results": 3,
            "include_raw_content": True,
        }
    ]
    assert reports == [
        (
            "网络搜索工具",
            {
                "query": "pytest",
                "topic": "news",
                "max_results": 3,
                "include_raw_content": True,
            },
        )
    ]


def test_search_preserves_empty_sdk_result(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _FakeTavilyClient({"query": "none", "results": []})
    monkeypatch.setattr(tavily_tool, "_create_tavily_client", lambda: client)
    monkeypatch.setattr(tavily_tool.monitor, "report_tool", lambda *args, **kwargs: None)

    result = tavily_tool.internet_search.invoke({"query": "none"})

    assert result == {"query": "none", "results": []}


def test_search_rejects_missing_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    monkeypatch.setattr(
        tavily_tool,
        "TavilyClient",
        lambda **_kwargs: pytest.fail("SDK must not be initialized without an API key"),
    )

    result = tavily_tool.internet_search.invoke({"query": "offline"})

    assert "未配置 TAVILY_API_KEY" in result


@pytest.mark.parametrize(
    ("factory", "expected"),
    [
        (lambda: (_ for _ in ()).throw(RuntimeError("init failed")), "初始化失败"),
        (lambda: _FakeTavilyClient(error=TimeoutError("timed out")), "搜索失败"),
    ],
)
def test_search_converts_sdk_failures_to_tool_errors(
    monkeypatch: pytest.MonkeyPatch,
    factory,
    expected: str,
) -> None:
    monkeypatch.setattr(tavily_tool, "_create_tavily_client", factory)
    monkeypatch.setattr(tavily_tool.monitor, "report_tool", lambda *args, **kwargs: None)

    result = tavily_tool.internet_search.invoke({"query": "failure"})

    assert result.startswith("Error:")
    assert expected in result

