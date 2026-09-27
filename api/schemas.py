from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class TaskRequest(BaseModel):
    query: str
    thread_id: str | None = None
    request_id: str | None = Field(default=None, min_length=1, max_length=255)


class TaskResponse(BaseModel):
    status: str
    thread_id: str
    run_id: str
    request_id: str
    deduplicated: bool


class RunRecoveryResponse(BaseModel):
    status: str
    thread_id: str
    run_id: str
    parent_run_id: str | None


class PersistenceResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class ThreadResponse(PersistenceResponse):
    id: str
    title: str | None
    status: str
    created_at: str
    updated_at: str


class RunResponse(PersistenceResponse):
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


class MessageResponse(PersistenceResponse):
    id: str
    thread_id: str
    run_id: str | None
    role: str
    content: str
    sequence: int
    created_at: str


class EventResponse(PersistenceResponse):
    id: int
    thread_id: str
    run_id: str | None
    event_type: str
    message: str
    data: dict[str, Any]
    created_at: str


class ArtifactResponse(PersistenceResponse):
    id: str
    thread_id: str
    run_id: str | None
    kind: str
    filename: str
    media_type: str | None
    size_bytes: int
    sha256: str
    created_at: str
