"""Persistence for replayable runtime events."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Any

from persistence.database import Database


@dataclass(frozen=True)
class EventRecord:
    id: int
    thread_id: str
    run_id: str | None
    event_type: str
    message: str
    data: dict[str, Any]
    created_at: str

    def to_payload(self) -> dict[str, Any]:
        return {
            "type": "monitor_event",
            "event_id": self.id,
            "run_id": self.run_id,
            "event": self.event_type,
            "message": self.message,
            "data": self.data,
            "timestamp": self.created_at,
        }


def _event_from_row(row: sqlite3.Row) -> EventRecord:
    payload = json.loads(row["payload_json"])
    return EventRecord(
        id=row["id"],
        thread_id=row["thread_id"],
        run_id=row["run_id"],
        event_type=row["event_type"],
        message=payload["message"],
        data=payload.get("data") or {},
        created_at=row["created_at"],
    )


class EventRepository:
    def __init__(self, database: Database):
        self.database = database

    async def append(
        self,
        thread_id: str,
        event_type: str,
        message: str,
        *,
        data: dict[str, Any] | None = None,
        run_id: str | None = None,
    ) -> EventRecord:
        payload_json = json.dumps(
            {"message": message, "data": data or {}},
            ensure_ascii=False,
        )
        async with self.database.write_transaction() as connection:
            cursor = await connection.execute(
                """
                INSERT INTO events (thread_id, run_id, event_type, payload_json)
                VALUES (?, ?, ?, ?)
                """,
                (thread_id, run_id, event_type, payload_json),
            )
            event_id = cursor.lastrowid
        row = await (
            await self.database.connection.execute(
                "SELECT * FROM events WHERE id = ?",
                (event_id,),
            )
        ).fetchone()
        return _event_from_row(row)

    async def list_after(
        self,
        thread_id: str,
        *,
        after_event_id: int,
    ) -> list[EventRecord]:
        rows = await (
            await self.database.connection.execute(
                """
                SELECT * FROM events
                WHERE thread_id = ? AND id > ?
                ORDER BY id
                """,
                (thread_id, after_event_id),
            )
        ).fetchall()
        return [_event_from_row(row) for row in rows]
