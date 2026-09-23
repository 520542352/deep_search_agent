from pydantic import BaseModel, Field


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

