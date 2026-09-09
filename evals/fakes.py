from collections.abc import Sequence
from typing import Any

from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import BaseMessage
from langchain_core.runnables import Runnable
from pydantic import Field


class ScriptedChatModel(FakeMessagesListChatModel):
    """Tool-capable fake chat model whose responses are supplied by the test."""

    bound_tool_names: list[str] = Field(default_factory=list)

    def bind_tools(
        self,
        tools: Sequence[Any],
        *,
        tool_choice: str | None = None,
        **kwargs: Any,
    ) -> Runnable:
        self.bound_tool_names = [
            getattr(tool, "name", None)
            or (tool.get("name") if isinstance(tool, dict) else None)
            or getattr(tool, "__name__", type(tool).__name__)
            for tool in tools
        ]
        return self


class ScriptedEventAgent:
    """Small offline event source used to exercise the same runner as live Agents."""

    def __init__(
        self,
        events: list[dict[str, Any]],
        *,
        error: Exception | None = None,
    ) -> None:
        self.events = events
        self.error = error
        self.calls: list[tuple[dict[str, Any], dict[str, Any] | None, str]] = []

    async def astream_events(
        self,
        input: dict[str, Any],
        config: dict[str, Any] | None = None,
        *,
        version: str = "v2",
    ):
        self.calls.append((input, config, version))
        for event in self.events:
            yield event
        if self.error is not None:
            raise self.error


def tool_start(name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "event": "on_tool_start",
        "name": name,
        "data": {"input": arguments or {}},
    }


def model_end(message: BaseMessage, *, subagent: bool = False) -> dict[str, Any]:
    return {
        "event": "on_chat_model_end",
        "data": {"output": message},
        "metadata": {"ls_agent_type": "subagent" if subagent else "root"},
    }
