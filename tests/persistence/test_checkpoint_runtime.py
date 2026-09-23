from pathlib import Path

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.graph import END, START, MessagesState, StateGraph

from agent.runtime import AgentRuntime, resolve_checkpoint_path


def _build_test_agent(checkpointer):
    builder = StateGraph(MessagesState)
    builder.add_node(
        "record",
        lambda _state: {"messages": [AIMessage(content="persisted")]},
    )
    builder.add_edge(START, "record")
    builder.add_edge("record", END)
    return builder.compile(checkpointer=checkpointer)


@pytest.mark.unit
def test_checkpoint_path_can_be_overridden_by_environment(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    configured_path = tmp_path / "state" / "checkpoints.db"

    assert resolve_checkpoint_path(project_root, {}) == (
        project_root / "data" / "checkpoints.db"
    )
    assert resolve_checkpoint_path(
        project_root,
        {"DEEP_SEARCH_CHECKPOINT_PATH": str(configured_path)},
    ) == configured_path


@pytest.mark.unit
async def test_agent_runtime_persists_graph_state_across_restarts(
    tmp_path: Path,
) -> None:
    checkpoint_path = tmp_path / "nested" / "checkpoints.db"
    config = {"configurable": {"thread_id": "thread-a"}}

    first = AgentRuntime(checkpoint_path, agent_factory=_build_test_agent)
    await first.start()
    try:
        await first.agent.ainvoke(
            {"messages": [HumanMessage(content="remember this")]},
            config=config,
        )
    finally:
        await first.close()

    second = AgentRuntime(checkpoint_path, agent_factory=_build_test_agent)
    await second.start()
    try:
        snapshot = await second.agent.aget_state(config)
        assert [message.content for message in snapshot.values["messages"]] == [
            "remember this",
            "persisted",
        ]
    finally:
        await second.close()


@pytest.mark.unit
async def test_agent_runtime_start_and_close_are_idempotent(tmp_path: Path) -> None:
    runtime = AgentRuntime(
        tmp_path / "checkpoints.db",
        agent_factory=_build_test_agent,
    )

    await runtime.start()
    first_agent = runtime.agent
    await runtime.start()

    assert runtime.agent is first_agent
    assert runtime.is_started is True

    await runtime.close()
    await runtime.close()

    assert runtime.is_started is False
    with pytest.raises(RuntimeError, match="not started"):
        _ = runtime.agent
