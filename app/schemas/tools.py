from datetime import datetime
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.schemas.search import ProjectSearchResultResponse, SearchFilters


class ProjectStatus(StrEnum):
    INTERESTED = "INTERESTED"
    TO_TRY = "TO_TRY"
    IN_PROGRESS = "IN_PROGRESS"
    COMPLETED = "COMPLETED"


class ToolSearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=500)
    top_k: int = Field(default=5, ge=1, le=10)
    filters: SearchFilters | None = None

    @field_validator("query")
    @classmethod
    def strip_query(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("query must not be blank")
        return stripped


class ToolSearchResponse(BaseModel):
    query: str
    projects: list[ProjectSearchResultResponse]


class ProjectNoteCreate(BaseModel):
    note: str = Field(min_length=1, max_length=2000)

    @field_validator("note")
    @classmethod
    def strip_note(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("note must not be blank")
        return stripped


class ProjectStatusUpdate(BaseModel):
    status: ProjectStatus


class ToolResponseModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class ProjectNoteResponse(ToolResponseModel):
    note_id: UUID
    note: str
    created_at: datetime


class SavedProjectResponse(ToolResponseModel):
    saved_project_id: UUID
    repo_id: int
    status: ProjectStatus
    saved_at: datetime
    updated_at: datetime


class ProjectEvidenceResponse(ToolResponseModel):
    chunk_id: str
    chunk_index: int
    chunk_text: str


class ProjectDetailsResponse(ToolResponseModel):
    repo_id: int
    name: str
    full_name: str
    owner: str
    description: str | None
    html_url: str
    primary_language: str | None
    stars: int
    forks: int
    open_issues: int
    topics: list[str]
    license: str | None
    evidence: list[ProjectEvidenceResponse]
    saved_project: SavedProjectResponse | None
    notes: list[ProjectNoteResponse]
