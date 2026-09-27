"""Persistent lifecycle wrapper for agent executions."""

from collections.abc import Awaitable, Callable
from typing import Any

from api.context import (
    reset_run_context,
    reset_thread_context,
    set_run_context,
    set_thread_context,
)
from persistence.repositories import InvalidRunTransitionError


class AgentRunner:
    def __init__(
        self,
        repository,
        *,
        agent: Any,
        execute: Callable[..., Awaitable[str]],
        resume_execute: Callable[..., Awaitable[str]] | None = None,
        artifact_service=None,
    ) -> None:
        self.repository = repository
        self.agent = agent
        self.execute = execute
        self.resume_execute = resume_execute
        self.artifact_service = artifact_service

    async def run(self, run_id: str) -> None:
        run = await self.repository.mark_running(run_id)
        await self._execute_run(
            run,
            self.execute(
                run.query,
                run.thread_id,
                agent=self.agent,
                checkpoint_id=run.base_checkpoint_id,
            ),
        )

    async def resume(self, run_id: str, *, checkpoint_id: str) -> None:
        if self.resume_execute is None:
            raise RuntimeError("resume executor is not configured")
        run = await self.repository.get_run(run_id)
        if run.status != "running":
            raise InvalidRunTransitionError(
                f"run {run_id!r} was not claimed for resume"
            )
        await self._execute_run(
            run,
            self.resume_execute(
                run.thread_id,
                agent=self.agent,
                checkpoint_id=checkpoint_id,
            ),
        )

    async def _execute_run(self, run, operation: Awaitable[str]) -> None:
        run_id = run.id
        run_token = set_run_context(run_id)
        thread_token = set_thread_context(run.thread_id)
        try:
            result = await operation
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
