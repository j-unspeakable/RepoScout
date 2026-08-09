from datetime import datetime
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator


class IndexingRequestStatus(StrEnum):
    NEW = "NEW"
    REVIEWED = "REVIEWED"
    COVERED = "COVERED"
    DECLINED = "DECLINED"


class IndexingRequestCreate(BaseModel):
    search_query: str = Field(min_length=1, max_length=500)
    notes: str | None = Field(default=None, max_length=2000)

    @field_validator("search_query")
    @classmethod
    def strip_search_query(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("search_query must not be blank")
        return stripped

    @field_validator("notes")
    @classmethod
    def strip_optional_notes(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        return stripped or None


class IndexingRequestResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    request_id: UUID
    search_query: str
    notes: str | None
    status: IndexingRequestStatus
    created_at: datetime
