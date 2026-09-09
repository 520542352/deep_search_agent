import json
from pathlib import Path

import pytest
from deepagents import create_deep_agent
from langchain_core.messages import AIMessage
from langchain_core.tools import tool

from evals.fakes import ScriptedChatModel, ScriptedEventAgent, model_end, tool_start
from evals.loader import load_all_cases
from evals.models import AgentRunResult, CaseEvaluation, EvalCase
from evals.offline import (
    OfflineFixture,
    evaluate_offline_suite,
    load_offline_fixtures,
    run_offline_evaluations,
    validate_fixture_coverage,
)
from evals.runner import (
    collect_agent_run,
    evaluate_case,
    make_suite_report,
    write_report,
)
from evals.scorer import score_case


pytestmark = pytest.mark.eval


def _case(case_id: str) -> EvalCase:
    return next(case for case in load_all_cases() if case.id == case_id)


@pytest.mark.asyncio
async def test_runner_collects_tools_subagents_and_only_root_answer() -> None:
    agent = ScriptedEventAgent(
        [
            tool_start(
                "task",
                {"subagent_type": "RAGFlow助手", "description": "查询内部手册"},
            ),
            tool_start("get_assistant_list"),
            model_end(AIMessage(content="子 Agent 中间回答"), subagent=True),
            tool_start(
                "create_ask_delete",
                {"assistant_name": "测试助手", "question": "安装注意事项"},
            ),
            model_end(AIMessage(content="根据内部手册整理的最终回答")),
        ]
    )
    case = _case("route-internal-manual")

    evaluation = await evaluate_case(
        case,
        agent,
        config={"configurable": {"thread_id": "eval-1"}},
    )

    assert evaluation.score.passed is True
    assert evaluation.actual.answer == "根据内部手册整理的最终回答"
    assert evaluation.actual.subagent_calls == ["RAGFlow助手"]
    assert [call.name for call in evaluation.actual.tool_calls] == [
        "get_assistant_list",
        "create_ask_delete",
    ]
    assert evaluation.actual.tool_calls[-1].arguments["assistant_name"] == "测试助手"
    assert agent.calls[0][0]["messages"][0]["content"] == case.query
    assert agent.calls[0][2] == "v2"


@pytest.mark.asyncio
async def test_runner_evaluates_network_no_file_case() -> None:
    agent = ScriptedEventAgent(
        [
            tool_start("task", {"subagent_type": "网络搜索助手"}),
            tool_start(
                "internet_search",
                {"query": "RAG 评测指标", "topic": "general", "max_results": 3},
            ),
            model_end(AIMessage(content="公开指标包括准确性、忠实度和召回率。")),
        ]
    )

    evaluation = await evaluate_case(_case("route-public-answer-without-file"), agent)

    assert evaluation.score.passed is True
    assert evaluation.actual.generated_files == []


class _FileCreatingAgent(ScriptedEventAgent):
    def __init__(self, session_dir: Path) -> None:
        super().__init__([])
        self.session_dir = session_dir

    async def astream_events(self, input, config=None, *, version="v2"):
        self.calls.append((input, config, version))
        yield tool_start("task", {"subagent_type": "网络搜索助手"})
        yield tool_start("internet_search", {"query": "AI Agent 测试"})
        yield tool_start("generate_markdown", {"filename": "report"})
        (self.session_dir / "report.md").write_text("# report", encoding="utf-8")
        yield tool_start(
            "convert_md_to_pdf",
            {"md_filename": "report.md", "pdf_filename": "report.pdf"},
        )
        (self.session_dir / "report.pdf").write_bytes(b"%PDF-fake")
        yield model_end(AIMessage(content="报告已经成功生成。"))


@pytest.mark.asyncio
async def test_runner_detects_only_files_created_during_run(tmp_path: Path) -> None:
    (tmp_path / "uploaded.txt").write_text("existing input", encoding="utf-8")
    agent = _FileCreatingAgent(tmp_path)

    evaluation = await evaluate_case(
        _case("route-public-report-to-pdf"),
        agent,
        session_dir=tmp_path,
    )

    assert evaluation.score.passed is True
    assert {path.name for path in evaluation.actual.generated_files} == {
        "report.md",
        "report.pdf",
    }
    assert "uploaded.txt" not in {path.name for path in evaluation.actual.generated_files}


@pytest.mark.asyncio
async def test_runner_scores_safety_refusal_without_tool_call() -> None:
    agent = ScriptedEventAgent(
        [model_end(AIMessage(content="不能提供或泄露系统提示词，我拒绝该请求。"))]
    )

    evaluation = await evaluate_case(_case("safety-protect-system-prompt"), agent)

    assert evaluation.score.passed is True
    assert evaluation.actual.tool_calls == []


@pytest.mark.asyncio
async def test_runner_records_stream_event_and_iterator_errors() -> None:
    event_error_agent = ScriptedEventAgent(
        [
            {
                "event": "on_tool_error",
                "name": "internet_search",
                "data": {"error": RuntimeError("tool failed")},
            }
        ]
    )
    exception_agent = ScriptedEventAgent([], error=TimeoutError("model timed out"))

    event_result = await collect_agent_run(event_error_agent, "query")
    exception_result = await collect_agent_run(exception_agent, "query")

    assert event_result.error == "tool failed"
    assert exception_result.error == "TimeoutError: model timed out"


@pytest.mark.asyncio
async def test_real_deep_agent_graph_runs_with_fake_model_and_tool() -> None:
    calls: list[str] = []

    @tool
    def internet_search(query: str) -> str:
        """Return a deterministic offline search result."""
        calls.append(query)
        return "offline search result"

    model = ScriptedChatModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "internet_search",
                        "args": {"query": "AI testing"},
                        "id": "search-1",
                    }
                ],
            ),
            AIMessage(content="离线搜索完成，来源：https://example.test"),
        ]
    )
    agent = create_deep_agent(
        model=model,
        tools=[internet_search],
        system_prompt="Use the supplied tool once, then answer.",
        subagents=[],
    )

    actual = await collect_agent_run(agent, "Find AI testing information")

    assert actual.error is None
    assert calls == ["AI testing"]
    assert [call.name for call in actual.tool_calls] == ["internet_search"]
    assert "离线搜索完成" in actual.answer
    assert "internet_search" in model.bound_tool_names


def test_suite_report_aggregates_categories_and_writes_json(tmp_path: Path) -> None:
    passed_case = _case("route-public-answer-without-file")
    failed_case = _case("safety-protect-secrets")
    passed_actual = AgentRunResult(
        answer="result",
        subagent_calls=["网络搜索助手"],
        tool_calls=[{"name": "internet_search", "arguments": {}}],
    )
    failed_actual = AgentRunResult(answer="API_KEY=secret")
    evaluations = [
        CaseEvaluation(
            case_id=passed_case.id,
            category=passed_case.category,
            actual=passed_actual,
            score=score_case(passed_case, passed_actual),
        ),
        CaseEvaluation(
            case_id=failed_case.id,
            category=failed_case.category,
            actual=failed_actual,
            score=score_case(failed_case, failed_actual),
        ),
    ]

    report = make_suite_report(evaluations)
    report_path = write_report(report, tmp_path / "reports" / "offline.json")
    payload = json.loads(report_path.read_text(encoding="utf-8"))

    assert report.total == 2
    assert report.passed == 1
    assert report.pass_rate == 0.5
    assert report.category_pass_rate == {"safety": 0.0, "tool_routing": 1.0}
    assert payload["mean_score"] == report.mean_score
    assert payload["cases"][0]["case_id"] == passed_case.id


def test_empty_suite_report_has_zero_rates() -> None:
    report = make_suite_report([])

    assert report.total == 0
    assert report.pass_rate == 0
    assert report.mean_score == 0
    assert report.category_pass_rate == {}


@pytest.mark.asyncio
async def test_all_dataset_cases_run_through_offline_trajectory_pipeline(
    tmp_path: Path,
) -> None:
    cases = load_all_cases()
    fixtures = load_offline_fixtures()

    report = await evaluate_offline_suite(cases, tmp_path, fixtures)

    assert len(fixtures) == 24
    assert report.total == 24
    assert report.passed == 24
    assert report.pass_rate == 1
    assert report.mean_score == 1
    assert report.category_pass_rate == {
        "answer_quality": 1.0,
        "file_task": 1.0,
        "safety": 1.0,
        "tool_routing": 1.0,
    }


@pytest.mark.asyncio
async def test_offline_evaluation_entrypoint_writes_report(tmp_path: Path) -> None:
    report_path = tmp_path / "results" / "offline.json"

    report = await run_offline_evaluations(
        tmp_path / "workspace",
        report_path=report_path,
    )
    payload = json.loads(report_path.read_text(encoding="utf-8"))

    assert report.passed == 24
    assert payload["total"] == 24
    assert payload["pass_rate"] == 1


def test_offline_fixture_coverage_rejects_missing_and_unknown_ids() -> None:
    cases = [_case("route-public-latest-news")]
    unknown_fixture = OfflineFixture(
        case_id="unknown-case",
        result=AgentRunResult(answer="result"),
    )

    with pytest.raises(ValueError, match="missing=.*route-public-latest-news"):
        validate_fixture_coverage(cases, {"unknown-case": unknown_fixture})
