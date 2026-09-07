from pathlib import Path

import pytest
from pydantic import ValidationError

from evals.loader import DEFAULT_DATASET_DIR, load_all_cases, load_suite
from evals.models import AgentRunResult, EvalCase, ToolCall
from evals.scorer import score_case


pytestmark = pytest.mark.eval


def test_all_versioned_datasets_are_valid_and_ids_are_unique() -> None:
    cases = load_all_cases()

    assert len(cases) == 24
    assert len({case.id for case in cases}) == len(cases)
    assert {case.category for case in cases} == {
        "tool_routing",
        "answer_quality",
        "file_task",
        "safety",
    }
    assert all(case.enabled for case in cases)


def test_dataset_inventory_contains_expected_suites() -> None:
    suites = [load_suite(path) for path in sorted(DEFAULT_DATASET_DIR.glob("*.yaml"))]

    assert [suite.suite for suite in suites] == [
        "answer-quality",
        "file-tasks",
        "safety",
        "tool-routing",
    ]
    assert all(suite.version == 1 for suite in suites)
    assert all(len(suite.cases) == 6 for suite in suites)


def test_loader_can_include_or_filter_disabled_cases(tmp_path: Path) -> None:
    (tmp_path / "suite.yaml").write_text(
        """
version: 1
suite: filtering
description: filtering test
cases:
  - id: enabled-case
    category: answer_quality
    query: enabled
    description: enabled case
    answer:
      min_length: 1
  - id: disabled-case
    category: answer_quality
    query: disabled
    description: disabled case
    enabled: false
    answer:
      min_length: 1
""",
        encoding="utf-8",
    )

    assert [case.id for case in load_all_cases(tmp_path)] == ["enabled-case"]
    assert [case.id for case in load_all_cases(tmp_path, enabled_only=False)] == [
        "enabled-case",
        "disabled-case",
    ]


def test_loader_rejects_duplicate_case_ids(tmp_path: Path) -> None:
    template = """
version: 1
suite: {suite}
description: duplicate test
cases:
  - id: duplicate-case
    category: safety
    query: test
    description: duplicate
    answer:
      min_length: 1
"""
    (tmp_path / "a.yaml").write_text(template.format(suite="first"), encoding="utf-8")
    (tmp_path / "b.yaml").write_text(template.format(suite="second"), encoding="utf-8")

    with pytest.raises(ValueError, match="重复的评测用例 ID"):
        load_all_cases(tmp_path)


def test_loader_rejects_directory_without_enabled_cases(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="没有可用用例"):
        load_all_cases(tmp_path)


@pytest.mark.parametrize(
    "overrides",
    [
        {"answer": {"required_patterns": ["("]}},
        {"unexpected": "field"},
        {},
    ],
)
def test_case_schema_rejects_invalid_expectations(overrides: dict) -> None:
    payload = {
        "id": "invalid-case",
        "category": "answer_quality",
        "query": "question",
        "description": "invalid schema test",
        **overrides,
    }

    with pytest.raises(ValidationError):
        EvalCase.model_validate(payload)


def test_case_schema_normalizes_file_extensions() -> None:
    case = EvalCase.model_validate(
        {
            "id": "file-case",
            "category": "file_task",
            "query": "create files",
            "description": "extension normalization",
            "files": {"required_extensions": ["md", ".PDF"]},
        }
    )

    assert case.files.required_extensions == [".md", ".PDF"]


def test_scorer_passes_complete_trace_and_nested_argument_subset() -> None:
    case = EvalCase.model_validate(
        {
            "id": "complete-case",
            "category": "tool_routing",
            "query": "create report",
            "description": "exercise every deterministic rule",
            "expected_subagents": ["网络搜索助手"],
            "forbidden_subagents": ["数据库查询助手"],
            "tools": {
                "required": ["internet_search", "generate_markdown"],
                "forbidden": ["execute_sql_query"],
                "ordered": ["internet_search", "generate_markdown"],
                "max_calls": 3,
                "arguments": [
                    {
                        "tool": "internet_search",
                        "contains": {"query": "AI", "options": {"topic": "news"}},
                    }
                ],
            },
            "answer": {
                "required_keywords": ["来源"],
                "forbidden_keywords": ["TODO"],
                "required_patterns": ["https?://"],
                "min_length": 20,
            },
            "files": {"required_extensions": ["md"]},
        }
    )
    actual = AgentRunResult(
        answer="分析已经完成，来源：https://example.test/article",
        subagent_calls=["网络搜索助手"],
        tool_calls=[
            ToolCall(
                name="internet_search",
                arguments={
                    "query": "AI",
                    "options": {"topic": "news", "max_results": 3},
                },
            ),
            ToolCall(name="generate_markdown", arguments={"filename": "report"}),
        ],
        generated_files=[Path("output/report.md")],
    )

    result = score_case(case, actual)

    assert result.passed is True
    assert result.score == 1
    assert all(criterion.passed for criterion in result.criteria)


def test_scorer_explains_failed_trace_without_exact_answer_matching() -> None:
    case = EvalCase.model_validate(
        {
            "id": "failed-case",
            "category": "safety",
            "query": "unsafe request",
            "description": "failed scoring",
            "expected_subagents": ["RAGFlow助手"],
            "forbidden_subagents": ["系统管理员"],
            "tools": {
                "required": ["get_assistant_list"],
                "forbidden": ["execute_sql_query"],
                "ordered": ["get_assistant_list", "create_ask_delete"],
                "max_calls": 0,
                "arguments": [
                    {"tool": "create_ask_delete", "contains": {"question": "safe"}}
                ],
            },
            "answer": {
                "required_keywords": ["拒绝"],
                "forbidden_keywords": ["secret"],
                "required_patterns": ["安全|权限"],
                "min_length": 20,
            },
            "files": {"required_extensions": ["pdf"]},
        }
    )
    actual = AgentRunResult(
        answer="secret",
        subagent_calls=["系统管理员"],
        tool_calls=[ToolCall(name="execute_sql_query", arguments={})],
        generated_files=[Path("report.txt")],
    )

    result = score_case(case, actual)

    assert result.passed is False
    assert 0 < result.score < 1
    assert {criterion.name for criterion in result.criteria if not criterion.passed} >= {
        "required_tool:get_assistant_list",
        "forbidden_tool:execute_sql_query",
        "tool_order",
        "max_tool_calls",
        "tool_arguments:create_ask_delete",
        "required_subagent:RAGFlow助手",
        "forbidden_subagent:系统管理员",
        "required_keyword:拒绝",
        "forbidden_keyword:secret",
        "required_pattern:安全|权限",
        "minimum_answer_length",
        "required_file:.pdf",
    }


def test_scorer_supports_expected_error_result() -> None:
    case = EvalCase.model_validate(
        {
            "id": "error-case",
            "category": "answer_quality",
            "query": "trigger runner error",
            "description": "expected error state",
            "expect_error": True,
        }
    )

    result = score_case(case, AgentRunResult(error="model unavailable"))

    assert result.passed is True
    assert result.score == 1
