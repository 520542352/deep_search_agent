import re
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


ServiceName = Literal["llm", "tavily", "ragflow", "mysql"]
EvalCategory = Literal["tool_routing", "answer_quality", "file_task", "safety"]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ToolArgumentExpectation(StrictModel):
    tool: str = Field(min_length=1)
    contains: dict[str, Any] = Field(default_factory=dict)


class ToolExpectations(StrictModel):
    required: list[str] = Field(default_factory=list)
    forbidden: list[str] = Field(default_factory=list)
    ordered: list[str] = Field(default_factory=list)
    arguments: list[ToolArgumentExpectation] = Field(default_factory=list)
    max_calls: int | None = Field(default=None, ge=0)


class AnswerExpectations(StrictModel):
    required_keywords: list[str] = Field(default_factory=list)
    forbidden_keywords: list[str] = Field(default_factory=list)
    required_patterns: list[str] = Field(default_factory=list)
    min_length: int | None = Field(default=None, ge=0)

    @field_validator("required_patterns")
    @classmethod
    def validate_patterns(cls, patterns: list[str]) -> list[str]:
        for pattern in patterns:
            try:
                re.compile(pattern)
            except re.error as exc:
                raise ValueError(f"无效的正则表达式 {pattern!r}: {exc}") from exc
        return patterns


class FileExpectations(StrictModel):
    required_extensions: list[str] = Field(default_factory=list)

    @field_validator("required_extensions")
    @classmethod
    def normalize_extensions(cls, extensions: list[str]) -> list[str]:
        return [extension if extension.startswith(".") else f".{extension}" for extension in extensions]


class EvalCase(StrictModel):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]*$")
    category: EvalCategory
    query: str = Field(min_length=1)
    description: str = Field(min_length=1)
    enabled: bool = True
    tags: list[str] = Field(default_factory=list)
    required_services: list[ServiceName] = Field(default_factory=list)
    required_test_data: list[str] = Field(default_factory=list)
    expected_subagents: list[str] = Field(default_factory=list)
    forbidden_subagents: list[str] = Field(default_factory=list)
    tools: ToolExpectations = Field(default_factory=ToolExpectations)
    answer: AnswerExpectations = Field(default_factory=AnswerExpectations)
    files: FileExpectations = Field(default_factory=FileExpectations)
    expect_error: bool = False

    @model_validator(mode="after")
    def require_an_assertion(self):
        has_expectation = any(
            (
                self.expected_subagents,
                self.forbidden_subagents,
                self.tools.required,
                self.tools.forbidden,
                self.tools.ordered,
                self.tools.arguments,
                self.tools.max_calls is not None,
                self.answer.required_keywords,
                self.answer.forbidden_keywords,
                self.answer.required_patterns,
                self.answer.min_length is not None,
                self.files.required_extensions,
                self.expect_error,
            )
        )
        if not has_expectation:
            raise ValueError("评测用例至少需要一项可验证的期望")
        return self


class EvalSuite(StrictModel):
    version: Literal[1]
    suite: str = Field(min_length=1)
    description: str = Field(min_length=1)
    cases: list[EvalCase] = Field(min_length=1)


class ToolCall(StrictModel):
    name: str = Field(min_length=1)
    arguments: dict[str, Any] = Field(default_factory=dict)


class AgentRunResult(StrictModel):
    answer: str = ""
    tool_calls: list[ToolCall] = Field(default_factory=list)
    subagent_calls: list[str] = Field(default_factory=list)
    generated_files: list[Path] = Field(default_factory=list)
    error: str | None = None


class CriterionResult(StrictModel):
    name: str
    passed: bool
    detail: str


class CaseScore(StrictModel):
    case_id: str
    passed: bool
    score: float = Field(ge=0, le=1)
    criteria: list[CriterionResult]
