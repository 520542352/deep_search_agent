"""Durable runtime event publishing."""

from typing import Any


class EventPublisher:
    def __init__(self, repository, manager):
        self.repository = repository
        self.manager = manager

    async def publish(
        self,
        thread_id: str,
        event_type: str,
        message: str,
        *,
        data: dict[str, Any] | None = None,
        run_id: str | None = None,
    ):
        event = await self.repository.append(
            thread_id,
            event_type,
            message,
            data=data,
            run_id=run_id,
        )
        await self.manager.send_to_thread(event.to_payload(), thread_id)
        return event
