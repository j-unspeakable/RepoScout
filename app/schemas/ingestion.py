from datetime import datetime
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, Field, field_validator


class IngestionStatus(StrEnum):
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class IngestionRequest(BaseModel):
    search_query: str = Field(min_length=1, max_length=256)
    max_repositories: int = Field(default=30, ge=1, le=100)

    @field_validator("search_query")
    @classmethod
    def strip_search_query(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("search_query must not be blank")
        return value


class IngestionRunResponse(BaseModel):
    run_id: UUID
    search_query: str
    started_at: datetime
    completed_at: datetime | None
    repositories_found: int
    repositories_inserted: int
    repositories_updated: int
    status: IngestionStatus
    error_message: str | None
