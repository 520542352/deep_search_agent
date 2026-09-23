from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import aiosqlite
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver


AgentFactory = Callable[[BaseCheckpointSaver], Any]


def resolve_checkpoint_path(
    project_root: Path,
    environ: Mapping[str, str] | None = None,
) -> Path:
    environment = os.environ if environ is None else environ
    configured_path = environment.get("DEEP_SEARCH_CHECKPOINT_PATH")
    if configured_path:
        return Path(configured_path).expanduser()
    return Path(project_root) / "data" / "checkpoints.db"


class AgentRuntime:
    def __init__(self, path: Path, *, agent_factory: AgentFactory) -> None:
        self.path = Path(path)
        self._agent_factory = agent_factory
        self._connection: aiosqlite.Connection | None = None
        self._checkpointer: AsyncSqliteSaver | None = None
        self._agent: Any | None = None

    @property
    def is_started(self) -> bool:
        return self._connection is not None

    @property
    def agent(self) -> Any:
        if self._agent is None:
            raise RuntimeError("agent runtime is not started")
        return self._agent

    @property
    def checkpointer(self) -> AsyncSqliteSaver:
        if self._checkpointer is None:
            raise RuntimeError("agent runtime is not started")
        return self._checkpointer

    async def start(self) -> None:
        if self.is_started:
            return

        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = await aiosqlite.connect(self.path)
        serializer = JsonPlusSerializer(allowed_msgpack_modules=None)
        checkpointer = AsyncSqliteSaver(connection, serde=serializer)

        try:
            await checkpointer.setup()
            agent = self._agent_factory(checkpointer)
        except Exception:
            await connection.close()
            raise

        self._connection = connection
        self._checkpointer = checkpointer
        self._agent = agent

    async def close(self) -> None:
        connection = self._connection
        self._connection = None
        self._checkpointer = None
        self._agent = None
        if connection is not None:
            await connection.close()
