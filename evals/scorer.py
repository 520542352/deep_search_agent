import re
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from evals.models import AgentRunResult, CaseScore, CriterionResult, EvalCase


def _is_subsequence(expected: list[str], actual: list[str]) -> bool:
    iterator: Iterable[str] = iter(actual)
    return all(any(item == expected_item for item in iterator) for expected_item in expected)


def _contains_subset(actual: Any, expected: Any) -> bool:
    if isinstance(expected, dict):
        return isinstance(actual, dict) and all(
            key in actual and _contains_subset(actual[key], value)
            for key, value in expected.items()
        )
    if isinstance(expected, list):
        return isinstance(actual, list) and all(item in actual for item in expected)
    return actual == expected


def score_case(case: EvalCase, actual: AgentRunResult) -> CaseScore:
    """Score deterministic trace, content and file invariants for one Agent run."""
    criteria: list[CriterionResult] = []

    def check(name: str, passed: bool, detail: str) -> None:
        criteria.append(CriterionResult(name=name, passed=passed, detail=detail))

    tool_names = [call.name for call in actual.tool_calls]
    for tool_name in case.tools.required:
        check(
            f"required_tool:{tool_name}",
            tool_name in tool_names,
            f"实际工具序列: {tool_names}",
        )
    for tool_name in case.tools.forbidden:
        check(
            f"forbidden_tool:{tool_name}",
            tool_name not in tool_names,
            f"实际工具序列: {tool_names}",
        )
    if case.tools.ordered:
        check(
            "tool_order",
            _is_subsequence(case.tools.ordered, tool_names),
            f"期望顺序: {case.tools.ordered}; 实际: {tool_names}",
        )
    if case.tools.max_calls is not None:
        check(
            "max_tool_calls",
            len(tool_names) <= case.tools.max_calls,
            f"最多 {case.tools.max_calls} 次; 实际 {len(tool_names)} 次",
        )
    for expectation in case.tools.arguments:
        matching_calls = [call for call in actual.tool_calls if call.name == expectation.tool]
        passed = any(
            _contains_subset(call.arguments, expectation.contains) for call in matching_calls
        )
        check(
            f"tool_arguments:{expectation.tool}",
            passed,
            f"期望参数子集: {expectation.contains}",
        )

    for subagent in case.expected_subagents:
        check(
            f"required_subagent:{subagent}",
            subagent in actual.subagent_calls,
            f"实际子 Agent: {actual.subagent_calls}",
        )
    for subagent in case.forbidden_subagents:
        check(
            f"forbidden_subagent:{subagent}",
            subagent not in actual.subagent_calls,
            f"实际子 Agent: {actual.subagent_calls}",
        )

    folded_answer = actual.answer.casefold()
    for keyword in case.answer.required_keywords:
        check(
            f"required_keyword:{keyword}",
            keyword.casefold() in folded_answer,
            f"回答必须包含: {keyword}",
        )
    for keyword in case.answer.forbidden_keywords:
        check(
            f"forbidden_keyword:{keyword}",
            keyword.casefold() not in folded_answer,
            f"回答不得包含: {keyword}",
        )
    for pattern in case.answer.required_patterns:
        check(
            f"required_pattern:{pattern}",
            re.search(pattern, actual.answer, re.IGNORECASE) is not None,
            f"回答必须匹配: {pattern}",
        )
    if case.answer.min_length is not None:
        check(
            "minimum_answer_length",
            len(actual.answer.strip()) >= case.answer.min_length,
            f"最少 {case.answer.min_length} 字符; 实际 {len(actual.answer.strip())}",
        )

    actual_extensions = {Path(path).suffix.lower() for path in actual.generated_files}
    for extension in case.files.required_extensions:
        check(
            f"required_file:{extension}",
            extension.lower() in actual_extensions,
            f"实际文件扩展名: {sorted(actual_extensions)}",
        )

    check(
        "error_state",
        (actual.error is not None) == case.expect_error,
        f"期望错误={case.expect_error}; 实际错误={actual.error!r}",
    )
    passed_count = sum(criterion.passed for criterion in criteria)
    score = passed_count / len(criteria)
    return CaseScore(
        case_id=case.id,
        passed=passed_count == len(criteria),
        score=score,
        criteria=criteria,
    )
