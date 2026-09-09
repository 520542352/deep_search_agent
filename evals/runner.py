import json
from collections import defaultdict
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, Protocol

from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage

from evals.models import (
    AgentRunResult,
    CaseEvaluation,
    EvalCase,
    SuiteReport,
    ToolCall,
)
from evals.scorer import score_case


class EventStreamingAgent(Protocol):
    def astream_events(
        self,
        input: dict[str, Any],
        config: dict[str, Any] | None = None,
        *,
        version: str = "v2",
    ) -> AsyncIterator[dict[str, Any]]: ...


def _message_from_output(output: Any) -> BaseMessage | None:
    if isinstance(output, BaseMessage):
        return output
    generations = getattr(output, "generations", None)
    if generations and generations[0]:
        first_generation = generations[0]
        if isinstance(first_generation, list):
            first_generation = first_generation[0]
        message = getattr(first_generation, "message", None)
        if isinstance(message, BaseMessage):
            return message
    if isinstance(output, dict):
        messages = output.get("messages")
        if isinstance(messages, list) and messages and isinstance(messages[-1], BaseMessage):
            return messages[-1]
    return None


def _message_text(message: BaseMessage | None) -> str:
    if not isinstance(message, (AIMessage, AIMessageChunk)):
        return ""
    text = message.text
    return text.strip() if text else ""


def _snapshot_files(session_dir: Path | None) -> set[Path]:
    if session_dir is None or not session_dir.exists():
        return set()
    return {path.resolve() for path in session_dir.rglob("*") if path.is_file()}


async def collect_agent_run(
    agent: EventStreamingAgent,
    query: str,
    *,
    config: dict[str, Any] | None = None,
    session_dir: str | Path | None = None,
) -> AgentRunResult:
    """Execute an event-streaming Agent and normalize its observable trajectory."""
    resolved_session = Path(session_dir).resolve() if session_dir is not None else None
    files_before = _snapshot_files(resolved_session)
    tool_calls: list[ToolCall] = []
    subagent_calls: list[str] = []
    answer = ""
    error: str | None = None

    try:
        async for event in agent.astream_events(
            {"messages": [{"role": "user", "content": query}]},
            config=config,
            version="v2",
        ):
            event_name = event.get("event")
            data = event.get("data") or {}
            metadata = event.get("metadata") or {}

            if event_name == "on_tool_start":
                tool_name = str(event.get("name") or "")
                arguments = data.get("input")
                arguments = arguments if isinstance(arguments, dict) else {}
                if tool_name == "task":
                    subagent_type = arguments.get("subagent_type")
                    if isinstance(subagent_type, str) and subagent_type:
                        subagent_calls.append(subagent_type)
                elif tool_name:
                    tool_calls.append(ToolCall(name=tool_name, arguments=arguments))

            elif event_name == "on_chat_model_end" and metadata.get("ls_agent_type") != "subagent":
                candidate = _message_text(_message_from_output(data.get("output")))
                if candidate:
                    answer = candidate

            elif event_name in {"on_chain_error", "on_tool_error", "on_chat_model_error"}:
                event_error = data.get("error")
                error = str(event_error or event_name)
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"

    files_after = _snapshot_files(resolved_session)
    generated_files = sorted(files_after - files_before, key=str)
    return AgentRunResult(
        answer=answer,
        tool_calls=tool_calls,
        subagent_calls=subagent_calls,
        generated_files=generated_files,
        error=error,
    )


async def evaluate_case(
    case: EvalCase,
    agent: EventStreamingAgent,
    *,
    config: dict[str, Any] | None = None,
    session_dir: str | Path | None = None,
) -> CaseEvaluation:
    actual = await collect_agent_run(
        agent,
        case.query,
        config=config,
        session_dir=session_dir,
    )
    return CaseEvaluation(
        case_id=case.id,
        category=case.category,
        actual=actual,
        score=score_case(case, actual),
    )


def make_suite_report(evaluations: list[CaseEvaluation]) -> SuiteReport:
    category_results: dict[str, list[bool]] = defaultdict(list)
    for evaluation in evaluations:
        category_results[evaluation.category].append(evaluation.score.passed)

    total = len(evaluations)
    passed = sum(evaluation.score.passed for evaluation in evaluations)
    mean_score = (
        sum(evaluation.score.score for evaluation in evaluations) / total if total else 0
    )
    return SuiteReport(
        total=total,
        passed=passed,
        pass_rate=passed / total if total else 0,
        mean_score=mean_score,
        category_pass_rate={
            category: sum(results) / len(results)
            for category, results in sorted(category_results.items())
        },
        cases=evaluations,
    )


def write_report(report: SuiteReport, path: str | Path) -> Path:
    report_path = Path(path)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        json.dumps(report.model_dump(mode="json"), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return report_path
