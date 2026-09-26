"""Persistent lifecycle wrapper for agent executions."""

from collections.abc import Awaitable, Callable
from typing import Any

from api.context import (
    reset_run_context,
    reset_thread_context,
    set_run_context,
    set_thread_context,
)


class AgentRunner:
    def __init__(
        self,
        repository,
        *,
        agent: Any,
        execute: Callable[..., Awaitable[str]],
        artifact_service=None,
    ) -> None:
        self.repository = repository
        self.agent = agent
        self.execute = execute
        self.artifact_service = artifact_service

    async def run(self, run_id: str) -> None:
        run = await self.repository.mark_running(run_id)
        run_token = set_run_context(run_id)
        thread_token = set_thread_context(run.thread_id)
        try:
            result = await self.execute(
                run.query,
                run.thread_id,
                agent=self.agent,
            )
            if self.artifact_service is not None:
                await self.artifact_service.index_generated(run.thread_id, run.id)
        except Exception as error:
            await self.repository.mark_failed(run_id, error)
            raise
        else:
            await self.repository.mark_succeeded(run_id, result)
        finally:
            reset_thread_context(thread_token)
            reset_run_context(run_token)
