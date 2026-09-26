"""Transactional persistence for conversation and run lifecycle data."""

from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass

from persistence.database import Database


class PersistenceConflictError(RuntimeError):
    """Base error for requests that conflict with persisted state."""


class ActiveRunConflictError(PersistenceConflictError):
    """Raised when a thread already has a queued or running run."""


class RecordNotFoundError(LookupError):
    """Raised when a requested persistence record does not exist."""


class InvalidRunTransitionError(PersistenceConflictError):
    """Raised when a run cannot move from its current status."""


@dataclass(frozen=True)
class ThreadRecord:
    id: str
    title: str | None
    status: str
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class RunRecord:
    id: str
    thread_id: str
    parent_run_id: str | None
    request_id: str | None
    query: str
    status: str
    error_code: str | None
    error_message: str | None
    started_at: str | None
    finished_at: str | None
    created_at: str


@dataclass(frozen=True)
class MessageRecord:
    id: str
    thread_id: str
    run_id: str | None
    role: str
    content: str
    sequence: int
    created_at: str


@dataclass(frozen=True)
class SubmissionResult:
    run: RunRecord
    created: bool


def _run_from_row(row: sqlite3.Row) -> RunRecord:
    return RunRecord(**dict(row))


def _thread_from_row(row: sqlite3.Row) -> ThreadRecord:
    return ThreadRecord(**dict(row))


def _message_from_row(row: sqlite3.Row) -> MessageRecord:
    return MessageRecord(**dict(row))


class ConversationRepository:
    """Own atomic writes spanning threads, runs, and messages."""

    def __init__(self, database: Database):
        self.database = database

    async def get_thread(self, thread_id: str) -> ThreadRecord:
        row = await (
            await self.database.connection.execute(
                "SELECT * FROM threads WHERE id = ?",
                (thread_id,),
            )
        ).fetchone()
        if row is None:
            raise RecordNotFoundError(f"thread not found: {thread_id}")
        return _thread_from_row(row)

    async def list_threads(self) -> list[ThreadRecord]:
        rows = await (
            await self.database.connection.execute(
                "SELECT * FROM threads ORDER BY updated_at DESC, id"
            )
        ).fetchall()
        return [_thread_from_row(row) for row in rows]

    async def submit(
        self,
        thread_id: str,
        query: str,
        request_id: str,
    ) -> SubmissionResult:
        async with self.database.write_transaction() as connection:
            existing = await (
                await connection.execute(
                    "SELECT * FROM runs WHERE thread_id = ? AND request_id = ?",
                    (thread_id, request_id),
                )
            ).fetchone()
            if existing is not None:
                return SubmissionResult(_run_from_row(existing), created=False)

            active = await (
                await connection.execute(
                    """
                    SELECT id FROM runs
                    WHERE thread_id = ? AND status IN ('queued', 'running')
                    """,
                    (thread_id,),
                )
            ).fetchone()
            if active is not None:
                raise ActiveRunConflictError(
                    f"thread {thread_id!r} already has an active run"
                )

            await connection.execute(
                """
                INSERT INTO threads (id) VALUES (?)
                ON CONFLICT(id) DO UPDATE SET
                    updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                """,
                (thread_id,),
            )
            run_id = str(uuid.uuid4())
            await connection.execute(
                """
                INSERT INTO runs (id, thread_id, request_id, query, status)
                VALUES (?, ?, ?, ?, 'queued')
                """,
                (run_id, thread_id, request_id, query),
            )
            await self._append_message(
                thread_id=thread_id,
                run_id=run_id,
                role="user",
                content=query,
            )
        return SubmissionResult(await self.get_run(run_id), created=True)

    async def get_run(self, run_id: str) -> RunRecord:
        row = await (
            await self.database.connection.execute(
                "SELECT * FROM runs WHERE id = ?",
                (run_id,),
            )
        ).fetchone()
        if row is None:
            raise RecordNotFoundError(f"run not found: {run_id}")
        return _run_from_row(row)

    async def list_runs(self, thread_id: str) -> list[RunRecord]:
        rows = await (
            await self.database.connection.execute(
                "SELECT * FROM runs WHERE thread_id = ? ORDER BY created_at, id",
                (thread_id,),
            )
        ).fetchall()
        return [_run_from_row(row) for row in rows]

    async def list_messages(self, thread_id: str) -> list[MessageRecord]:
        rows = await (
            await self.database.connection.execute(
                "SELECT * FROM messages WHERE thread_id = ? ORDER BY sequence",
                (thread_id,),
            )
        ).fetchall()
        return [_message_from_row(row) for row in rows]

    async def mark_running(self, run_id: str) -> RunRecord:
        async with self.database.write_transaction() as connection:
            cursor = await connection.execute(
                """
                UPDATE runs
                SET status = 'running',
                    started_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                WHERE id = ? AND status = 'queued'
                """,
                (run_id,),
            )
            if cursor.rowcount != 1:
                raise InvalidRunTransitionError(f"run {run_id!r} is not queued")
        return await self.get_run(run_id)

    async def mark_succeeded(self, run_id: str, assistant_message: str) -> RunRecord:
        async with self.database.write_transaction() as connection:
            run = await self.get_run(run_id)
            if run.status != "running":
                raise InvalidRunTransitionError(f"run {run_id!r} is not running")
            if assistant_message:
                await self._append_message(
                    thread_id=run.thread_id,
                    run_id=run.id,
                    role="assistant",
                    content=assistant_message,
                )
            await connection.execute(
                """
                UPDATE runs
                SET status = 'succeeded',
                    finished_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                WHERE id = ?
                """,
                (run_id,),
            )
        return await self.get_run(run_id)

    async def mark_failed(self, run_id: str, error: Exception) -> RunRecord:
        async with self.database.write_transaction() as connection:
            cursor = await connection.execute(
                """
                UPDATE runs
                SET status = 'failed',
                    error_code = ?,
                    error_message = ?,
                    finished_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                WHERE id = ? AND status = 'running'
                """,
                (type(error).__name__, str(error), run_id),
            )
            if cursor.rowcount != 1:
                raise InvalidRunTransitionError(f"run {run_id!r} is not running")
        return await self.get_run(run_id)

    async def _append_message(
        self,
        *,
        thread_id: str,
        run_id: str,
        role: str,
        content: str,
    ) -> None:
        await self.database.connection.execute(
            """
            INSERT INTO messages (id, thread_id, run_id, role, content, sequence)
            SELECT ?, ?, ?, ?, ?, COALESCE(MAX(sequence), -1) + 1
            FROM messages
            WHERE thread_id = ?
            """,
            (str(uuid.uuid4()), thread_id, run_id, role, content, thread_id),
        )
