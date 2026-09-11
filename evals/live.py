"""Opt-in evaluation against the real model and external services.

This module is deliberately not imported by the default CI path. Running it may
consume API quota and read configured RAGFlow/MySQL test data.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import tempfile
from collections import defaultdict
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from dotenv import load_dotenv
from pydantic import Field

from evals.loader import load_all_cases
from evals.models import AgentRunResult, CaseEvaluation, EvalCase, StrictModel, SuiteReport
from evals.runner import evaluate_case, make_suite_report
from evals.scorer import score_case


DEFAULT_LIVE_DATASET_DIR = Path(__file__).with_name("live_datasets")
DEFAULT_REPORT_PATH = Path("eval-results/live-report.json")
SECRET_ENV_NAMES = (
    "OPENAI_API_KEY",
    "TAVILY_API_KEY",
    "RAGFLOW_API_KEY",
    "MYSQL_PASSWORD",
)
SERVICE_ENV = {
    "llm": ("LLM", "OPENAI_API_KEY", "OPENAI_BASE_URL"),
    "tavily": ("TAVILY_API_KEY",),
    "ragflow": ("RAGFLOW_API_URL", "RAGFLOW_API_KEY"),
    "mysql": ("MYSQL_HOST", "MYSQL_PORT", "MYSQL_USER", "MYSQL_PASSWORD", "MYSQL_DATABASE"),
}


class ServiceCheck(StrictModel):
    service: Literal["llm", "tavily", "ragflow", "mysql"]
    passed: bool
    detail: str


class PreflightReport(StrictModel):
    ready: bool
    checks: list[ServiceCheck]


class CaseReliability(StrictModel):
    case_id: str
    runs: int = Field(ge=1)
    passed_runs: int = Field(ge=0)
    pass_rate: float = Field(ge=0, le=1)
    mean_score: float = Field(ge=0, le=1)
    mean_duration_seconds: float = Field(ge=0)


class MetricBaseline(StrictModel):
    version: Literal[1] = 1
    pass_rate: float = Field(ge=0, le=1)
    mean_score: float = Field(ge=0, le=1)
    mean_duration_seconds: float = Field(ge=0)
    mean_tokens: float = Field(ge=0)
    category_pass_rate: dict[str, float]


class BaselineComparison(StrictModel):
    baseline_path: str
    found: bool
    pass_rate_delta: float | None = None
    mean_score_delta: float | None = None
    mean_duration_seconds_delta: float | None = None
    mean_tokens_delta: float | None = None


class LiveReport(StrictModel):
    version: Literal[1] = 1
    generated_at: str
    profile: str
    repeats: int = Field(ge=1)
    preflight: PreflightReport
    summary: SuiteReport
    reliability: list[CaseReliability]
    mean_duration_seconds: float = Field(ge=0)
    total_tokens: int = Field(ge=0)
    mean_tokens: float = Field(ge=0)
    estimated_cost_usd: float | None = Field(default=None, ge=0)
    baseline: BaselineComparison | None = None


def redact_text(value: str) -> str:
    """Remove configured secrets and common credential forms from reports/errors."""
    redacted = value
    for name in SECRET_ENV_NAMES:
        secret = os.getenv(name)
        if secret:
            redacted = redacted.replace(secret, f"<redacted:{name}>")
    redacted = re.sub(
        r"(?i)(api[_-]?key|password|authorization)(\s*[:=]\s*)([^\s,;]+)",
        r"\1\2<redacted>",
        redacted,
    )
    return redacted


def redact_payload(value: Any) -> Any:
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, dict):
        return {
            key: (
                "<redacted>"
                if re.search(r"(?i)(api[_-]?key|password|authorization|secret|token)", str(key))
                else redact_payload(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_payload(item) for item in value]
    return value


def _missing_env(service: str) -> list[str]:
    return [name for name in SERVICE_ENV[service] if not os.getenv(name)]


def _probe_llm() -> str:
    from agent.llm import model

    response = model.invoke("只回复 LIVE_OK")
    if "LIVE_OK" not in str(response.content):
        raise RuntimeError("模型未返回预期的最小响应")
    return "模型响应正常"


def _probe_tavily() -> str:
    from tools.tavily_tool import _create_tavily_client

    client = _create_tavily_client()
    result = client.search(query="OpenAI official", max_results=1)
    if not result.get("results"):
        raise RuntimeError("搜索没有返回结果")
    return "搜索响应正常"


def _probe_ragflow() -> str:
    from ragflow_sdk import RAGFlow

    assistants = RAGFlow(
        api_key=os.environ["RAGFLOW_API_KEY"],
        base_url=os.environ["RAGFLOW_API_URL"],
    ).list_chats()
    names = [assistant.name for assistant in assistants]
    if not names:
        raise RuntimeError("未找到可用助手")
    return f"可用助手: {', '.join(names)}"


def _probe_mysql() -> str:
    from mysql.connector import connect
    from tools.db_tools import get_db_config

    with connect(**get_db_config()) as connection:
        with connection.cursor() as cursor:
            cursor.execute("SHOW TABLES")
            tables = [str(row[0]) for row in cursor.fetchall()]
    if not tables:
        raise RuntimeError("数据库中没有表")
    return f"可用表: {', '.join(tables)}"


DEFAULT_PROBES: dict[str, Callable[[], str]] = {
    "llm": _probe_llm,
    "tavily": _probe_tavily,
    "ragflow": _probe_ragflow,
    "mysql": _probe_mysql,
}


async def run_preflight(
    services: set[str],
    probes: dict[str, Callable[[], str]] | None = None,
) -> PreflightReport:
    load_dotenv()
    selected_probes = probes or DEFAULT_PROBES
    checks: list[ServiceCheck] = []
    for service in sorted(services):
        missing = _missing_env(service)
        if missing:
            checks.append(ServiceCheck(
                service=service,
                passed=False,
                detail=f"缺少环境变量: {', '.join(missing)}",
            ))
            continue
        try:
            detail = await asyncio.to_thread(selected_probes[service])
            checks.append(ServiceCheck(service=service, passed=True, detail=redact_text(detail)))
        except Exception as exc:
            checks.append(ServiceCheck(
                service=service,
                passed=False,
                detail=redact_text(f"{type(exc).__name__}: {exc}"),
            ))
    return PreflightReport(ready=all(check.passed for check in checks), checks=checks)


class LiveAgentAdapter:
    """Bind the real graph to an isolated evaluation workspace and ContextVars."""

    def __init__(self, graph: Any, session_dir: Path, thread_id: str) -> None:
        self.graph = graph
        self.session_dir = session_dir.resolve()
        self.thread_id = thread_id

    async def astream_events(self, input, config=None, *, version="v2"):
        from api.context import reset_session_context, set_session_context, set_thread_context

        self.session_dir.mkdir(parents=True, exist_ok=True)
        session_token = set_session_context(self.session_dir.as_posix())
        thread_token = set_thread_context(self.thread_id)
        payload = dict(input)
        messages = [dict(message) for message in payload.get("messages", [])]
        if messages:
            messages[-1]["content"] += (
                "\n\n[真实评测工作目录]\n"
                f"所有读取和生成文件必须限制在当前会话目录；使用相对文件名。"
                f"当前目录：{self.session_dir.as_posix()}"
            )
        payload["messages"] = messages
        runtime_config = config or {"configurable": {"thread_id": self.thread_id}}
        try:
            async for event in self.graph.astream_events(
                payload,
                config=runtime_config,
                version=version,
            ):
                yield event
        finally:
            reset_session_context(session_token, thread_token)


def _sanitize_evaluation(evaluation: CaseEvaluation) -> CaseEvaluation:
    actual = evaluation.actual.model_copy(update={
        "answer": redact_text(evaluation.actual.answer),
        "error": redact_text(evaluation.actual.error) if evaluation.actual.error else None,
        "tool_calls": [
            call.model_copy(update={"arguments": redact_payload(call.arguments)})
            for call in evaluation.actual.tool_calls
        ],
        "generated_files": [Path(path.name) for path in evaluation.actual.generated_files],
    })
    score = evaluation.score.model_copy(update={
        "criteria": [
            criterion.model_copy(update={"detail": redact_text(criterion.detail)})
            for criterion in evaluation.score.criteria
        ]
    })
    return evaluation.model_copy(update={"actual": actual, "score": score})


def _make_reliability(evaluations: list[CaseEvaluation]) -> list[CaseReliability]:
    grouped: dict[str, list[CaseEvaluation]] = defaultdict(list)
    for evaluation in evaluations:
        grouped[evaluation.case_id].append(evaluation)
    return [
        CaseReliability(
            case_id=case_id,
            runs=len(runs),
            passed_runs=sum(run.score.passed for run in runs),
            pass_rate=sum(run.score.passed for run in runs) / len(runs),
            mean_score=sum(run.score.score for run in runs) / len(runs),
            mean_duration_seconds=sum(run.actual.duration_seconds for run in runs) / len(runs),
        )
        for case_id, runs in sorted(grouped.items())
    ]


def _baseline_from_report(report: LiveReport) -> MetricBaseline:
    return MetricBaseline(
        pass_rate=report.summary.pass_rate,
        mean_score=report.summary.mean_score,
        mean_duration_seconds=report.mean_duration_seconds,
        mean_tokens=report.mean_tokens,
        category_pass_rate=report.summary.category_pass_rate,
    )


def _compare_baseline(report: LiveReport, path: Path) -> BaselineComparison:
    if not path.exists():
        return BaselineComparison(baseline_path=str(path), found=False)
    baseline = MetricBaseline.model_validate_json(path.read_text(encoding="utf-8"))
    return BaselineComparison(
        baseline_path=str(path),
        found=True,
        pass_rate_delta=report.summary.pass_rate - baseline.pass_rate,
        mean_score_delta=report.summary.mean_score - baseline.mean_score,
        mean_duration_seconds_delta=report.mean_duration_seconds - baseline.mean_duration_seconds,
        mean_tokens_delta=report.mean_tokens - baseline.mean_tokens,
    )


def _estimated_cost(evaluations: list[CaseEvaluation]) -> float | None:
    input_rate = os.getenv("LIVE_EVAL_INPUT_USD_PER_MILLION")
    output_rate = os.getenv("LIVE_EVAL_OUTPUT_USD_PER_MILLION")
    if input_rate is None or output_rate is None:
        return None
    input_tokens = sum(item.actual.input_tokens for item in evaluations)
    output_tokens = sum(item.actual.output_tokens for item in evaluations)
    return input_tokens * float(input_rate) / 1_000_000 + output_tokens * float(output_rate) / 1_000_000


async def evaluate_live_suite(
    cases: list[EvalCase],
    workspace: str | Path,
    *,
    repeats: int = 1,
    timeout_seconds: float = 180,
    graph: Any | None = None,
    preflight: PreflightReport | None = None,
    baseline_path: str | Path | None = None,
) -> LiveReport:
    if repeats < 1:
        raise ValueError("repeats 必须大于等于 1")
    if graph is None:
        from agent.main_agent import main_agent

        graph = main_agent
    workspace_path = Path(workspace)
    evaluations: list[CaseEvaluation] = []
    for case in cases:
        for repeat_index in range(repeats):
            session_dir = workspace_path / case.id / f"run-{repeat_index + 1}"
            thread_id = f"live_{case.id}_{repeat_index + 1}_{uuid4().hex[:8]}"
            adapter = LiveAgentAdapter(graph, session_dir, thread_id)
            try:
                evaluation = await asyncio.wait_for(
                    evaluate_case(
                        case,
                        adapter,
                        config={"configurable": {"thread_id": thread_id}},
                        session_dir=session_dir,
                    ),
                    timeout=timeout_seconds,
                )
            except TimeoutError:
                actual = AgentRunResult(error=f"TimeoutError: exceeded {timeout_seconds:g}s")
                evaluation = CaseEvaluation(
                    case_id=case.id,
                    category=case.category,
                    actual=actual,
                    score=score_case(case, actual),
                )
            evaluations.append(_sanitize_evaluation(evaluation))

    summary = make_suite_report(evaluations)
    total_runs = len(evaluations)
    total_tokens = sum(item.actual.total_tokens for item in evaluations)
    report = LiveReport(
        generated_at=datetime.now(timezone.utc).isoformat(),
        profile=DEFAULT_LIVE_DATASET_DIR.name,
        repeats=repeats,
        preflight=preflight or PreflightReport(ready=True, checks=[]),
        summary=summary,
        reliability=_make_reliability(evaluations),
        mean_duration_seconds=(
            sum(item.actual.duration_seconds for item in evaluations) / total_runs
            if total_runs else 0
        ),
        total_tokens=total_tokens,
        mean_tokens=total_tokens / total_runs if total_runs else 0,
        estimated_cost_usd=_estimated_cost(evaluations),
    )
    if baseline_path is not None:
        report.baseline = _compare_baseline(report, Path(baseline_path))
    return report


def write_live_report(report: LiveReport, path: str | Path) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(report.model_dump_json(indent=2), encoding="utf-8")
    return target


def write_metric_baseline(report: LiveReport, path: str | Path) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(_baseline_from_report(report).model_dump_json(indent=2), encoding="utf-8")
    return target


def _parse_services(value: str) -> set[str]:
    services = {item.strip() for item in value.split(",") if item.strip()}
    unknown = services - set(SERVICE_ENV)
    if unknown:
        raise ValueError(f"未知服务: {', '.join(sorted(unknown))}")
    return services


async def _async_main(args: argparse.Namespace) -> int:
    services = _parse_services(args.services)
    preflight = await run_preflight(services)
    for check in preflight.checks:
        print(f"[{('PASS' if check.passed else 'FAIL')}] {check.service}: {check.detail}")
    if not preflight.ready:
        print("Live evaluation skipped: service preflight failed.")
        return 2
    if args.preflight_only:
        return 0

    cases = load_all_cases(args.dataset_dir)
    if args.case:
        wanted = set(args.case)
        cases = [case for case in cases if case.id in wanted]
        missing = wanted - {case.id for case in cases}
        if missing:
            raise ValueError(f"未找到用例: {', '.join(sorted(missing))}")
    unavailable = [
        case.id for case in cases if not set(case.required_services).issubset(services)
    ]
    if unavailable:
        raise ValueError(f"所选服务不足以运行用例: {', '.join(unavailable)}")

    with tempfile.TemporaryDirectory(prefix="deep-search-live-") as workspace:
        report = await evaluate_live_suite(
            cases,
            workspace,
            repeats=args.repeats,
            timeout_seconds=args.timeout,
            preflight=preflight,
            baseline_path=args.baseline,
        )
    write_live_report(report, args.report)
    if args.save_baseline:
        write_metric_baseline(report, args.save_baseline)
    print(
        f"Live evaluations: {report.summary.passed}/{report.summary.total} passed, "
        f"mean score={report.summary.mean_score:.2%}, "
        f"mean latency={report.mean_duration_seconds:.2f}s, "
        f"mean tokens={report.mean_tokens:.0f}, report={args.report}"
    )
    return 0 if report.summary.passed == report.summary.total else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="运行显式启用的真实 Agent 评测")
    parser.add_argument("--dataset-dir", default=str(DEFAULT_LIVE_DATASET_DIR))
    parser.add_argument("--services", default="llm,tavily,ragflow,mysql")
    parser.add_argument("--case", action="append", help="只运行指定 case id，可重复传入")
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--timeout", type=float, default=180)
    parser.add_argument("--report", default=str(DEFAULT_REPORT_PATH))
    parser.add_argument("--baseline", help="与已有指标基线 JSON 对比")
    parser.add_argument("--save-baseline", help="将本次汇总指标保存为基线")
    parser.add_argument("--preflight-only", action="store_true")
    return asyncio.run(_async_main(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
