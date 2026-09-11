import json
import asyncio
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage

from api.context import get_session_context, get_thread_context
from evals.fakes import ScriptedEventAgent, model_end
from evals.live import (
    LiveAgentAdapter,
    MetricBaseline,
    evaluate_live_suite,
    redact_payload,
    redact_text,
    run_preflight,
    _parse_services,
    write_live_report,
    write_metric_baseline,
)
from evals.models import EvalCase


pytestmark = pytest.mark.eval


def _case() -> EvalCase:
    return EvalCase.model_validate({
        "id": "live-test-case",
        "category": "answer_quality",
        "query": "test",
        "description": "live runner test",
        "required_services": ["llm"],
        "answer": {"required_keywords": ["LIVE_OK"]},
    })


@pytest.mark.asyncio
async def test_preflight_reports_missing_environment(monkeypatch) -> None:
    for name in ("LLM", "OPENAI_API_KEY", "OPENAI_BASE_URL"):
        monkeypatch.setenv(name, "")

    report = await run_preflight({"llm"}, probes={"llm": lambda: "unused"})

    assert report.ready is False
    assert report.checks[0].passed is False
    assert "OPENAI_API_KEY" in report.checks[0].detail


@pytest.mark.asyncio
async def test_preflight_runs_injected_probe_and_redacts_secret(monkeypatch) -> None:
    monkeypatch.setenv("LLM", "test-model")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://example.test/v1")
    monkeypatch.setenv("OPENAI_API_KEY", "live-secret-value")

    report = await run_preflight(
        {"llm"},
        probes={"llm": lambda: "connected with live-secret-value"},
    )

    assert report.ready is True
    assert "live-secret-value" not in report.checks[0].detail
    assert "<redacted:OPENAI_API_KEY>" in report.checks[0].detail


@pytest.mark.asyncio
async def test_preflight_isolates_probe_failure(monkeypatch) -> None:
    monkeypatch.setenv("TAVILY_API_KEY", "configured")

    def fail_probe() -> str:
        raise ConnectionError("service unavailable")

    report = await run_preflight({"tavily"}, probes={"tavily": fail_probe})

    assert report.ready is False
    assert report.checks[0].detail == "ConnectionError: service unavailable"


def test_redact_text_masks_configured_and_inline_credentials(monkeypatch) -> None:
    monkeypatch.setenv("MYSQL_PASSWORD", "database-secret")

    result = redact_text("database-secret password=another-secret")

    assert "database-secret" not in result
    assert "another-secret" not in result
    assert result.count("<redacted") == 2


def test_redact_payload_recurses_into_tool_arguments(monkeypatch) -> None:
    monkeypatch.setenv("TAVILY_API_KEY", "search-secret")

    result = redact_payload({"nested": ["search-secret", {"password": "unknown-secret"}]})

    assert result["nested"][0] == "<redacted:TAVILY_API_KEY>"
    assert result["nested"][1]["password"] == "<redacted>"


def test_parse_services_rejects_unknown_service() -> None:
    assert _parse_services("llm, mysql") == {"llm", "mysql"}
    with pytest.raises(ValueError, match="unknown"):
        _parse_services("llm,unknown")


class _ContextCheckingGraph(ScriptedEventAgent):
    async def astream_events(self, input, config=None, *, version="v2"):
        assert get_session_context() is not None
        assert get_thread_context() == "live_thread"
        assert "真实评测工作目录" in input["messages"][-1]["content"]
        yield model_end(AIMessage(content="LIVE_OK"))


@pytest.mark.asyncio
async def test_live_adapter_binds_and_resets_context(tmp_path: Path) -> None:
    adapter = LiveAgentAdapter(_ContextCheckingGraph([]), tmp_path, "live_thread")

    events = [
        event async for event in adapter.astream_events(
            {"messages": [{"role": "user", "content": "test"}]}
        )
    ]

    assert events
    assert get_session_context() is None
    assert get_thread_context() is None


@pytest.mark.asyncio
async def test_live_suite_repeats_aggregates_metrics_and_writes_baseline(
    tmp_path: Path,
) -> None:
    answer = AIMessage(
        content="LIVE_OK",
        usage_metadata={"input_tokens": 8, "output_tokens": 2, "total_tokens": 10},
    )
    graph = ScriptedEventAgent([model_end(answer)])

    report = await evaluate_live_suite(
        [_case()],
        tmp_path / "workspace",
        repeats=2,
        graph=graph,
    )
    report_path = write_live_report(report, tmp_path / "live.json")
    baseline_path = write_metric_baseline(report, tmp_path / "baseline.json")

    assert report.summary.total == 2
    assert report.summary.passed == 2
    assert report.reliability[0].pass_rate == 1
    assert report.total_tokens == 20
    assert report.mean_tokens == 10
    assert json.loads(report_path.read_text(encoding="utf-8"))["version"] == 1
    baseline = MetricBaseline.model_validate_json(baseline_path.read_text(encoding="utf-8"))
    assert baseline.pass_rate == 1


@pytest.mark.asyncio
async def test_live_suite_compares_existing_baseline(tmp_path: Path) -> None:
    baseline_path = tmp_path / "baseline.json"
    baseline_path.write_text(MetricBaseline(
        pass_rate=0.5,
        mean_score=0.5,
        mean_duration_seconds=0,
        mean_tokens=0,
        category_pass_rate={"answer_quality": 0.5},
    ).model_dump_json(), encoding="utf-8")

    report = await evaluate_live_suite(
        [_case()],
        tmp_path / "workspace",
        graph=ScriptedEventAgent([model_end(AIMessage(content="LIVE_OK"))]),
        baseline_path=baseline_path,
    )

    assert report.baseline is not None
    assert report.baseline.found is True
    assert report.baseline.pass_rate_delta == 0.5


@pytest.mark.asyncio
async def test_live_suite_reports_missing_baseline_and_estimates_cost(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("LIVE_EVAL_INPUT_USD_PER_MILLION", "2")
    monkeypatch.setenv("LIVE_EVAL_OUTPUT_USD_PER_MILLION", "4")
    message = AIMessage(
        content="LIVE_OK",
        usage_metadata={"input_tokens": 1_000, "output_tokens": 500, "total_tokens": 1_500},
    )

    report = await evaluate_live_suite(
        [_case()],
        tmp_path / "workspace",
        graph=ScriptedEventAgent([model_end(message)]),
        baseline_path=tmp_path / "missing.json",
    )

    assert report.baseline is not None
    assert report.baseline.found is False
    assert report.estimated_cost_usd == pytest.approx(0.004)


class _SlowGraph:
    async def astream_events(self, input, config=None, *, version="v2"):
        await asyncio.sleep(1)
        if False:
            yield {}


@pytest.mark.asyncio
async def test_live_suite_converts_timeout_to_scored_failure(tmp_path: Path) -> None:
    report = await evaluate_live_suite(
        [_case()],
        tmp_path,
        graph=_SlowGraph(),
        timeout_seconds=0.001,
    )

    actual = report.summary.cases[0].actual
    assert report.summary.passed == 0
    assert actual.error == "TimeoutError: exceeded 0.001s"


@pytest.mark.asyncio
async def test_runner_collects_provider_token_usage_metadata() -> None:
    from evals.runner import collect_agent_run

    message = AIMessage(
        content="LIVE_OK",
        response_metadata={
            "token_usage": {
                "prompt_tokens": 12,
                "completion_tokens": 3,
                "total_tokens": 15,
            }
        },
    )

    actual = await collect_agent_run(ScriptedEventAgent([model_end(message)]), "test")

    assert actual.input_tokens == 12
    assert actual.output_tokens == 3
    assert actual.total_tokens == 15
