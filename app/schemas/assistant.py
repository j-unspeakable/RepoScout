from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator


class AssistantMessageRequest(BaseModel):
    conversation_id: UUID | None = None
    message: str = Field(min_length=1, max_length=2000)

    @field_validator("message")
    @classmethod
    def strip_message(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("message must not be blank")
        return stripped


class AssistantStreamRequest(AssistantMessageRequest):
    turn_id: UUID


class AssistantProgressEvent(BaseModel):
    phase: Literal[
        "working",
        "searching_projects",
        "reviewing_details",
        "saving_projects",
        "updating_status",
        "adding_notes",
        "continuing",
        "finishing",
    ]


class AssistantStreamErrorEvent(BaseModel):
    status: int = Field(ge=400, le=599)
    detail: str = Field(min_length=1, max_length=500)
    uncertain: bool = False
    retry_after: int | None = Field(default=None, gt=0)


class AssistantMessageResponseMessage(BaseModel):
    role: Literal["assistant"] = "assistant"
    content: str
    presentation: Literal["cards", "references", "text"] = "text"
    evidence: list["AssistantEvidenceProjectResponse"] = Field(default_factory=list)


class AssistantEvidenceChunkResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    chunk_index: int = Field(ge=0)
    chunk_text: str = Field(min_length=1, max_length=4000)
    similarity: float | None = Field(default=None, ge=-1.0, le=1.0, allow_inf_nan=False)


class AssistantEvidenceProjectResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    repo_id: int = Field(gt=0)
    name: str = Field(min_length=1, max_length=200)
    full_name: str = Field(min_length=1, max_length=200)
    owner: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    html_url: str = Field(min_length=1, max_length=500)
    primary_language: str | None = Field(default=None, max_length=100)
    stars: int = Field(ge=0)
    forks: int = Field(ge=0)
    open_issues: int = Field(ge=0)
    topics: list[str] = Field(default_factory=list, max_length=8)
    license: str | None = Field(default=None, max_length=100)
    similarity: float | None = Field(default=None, ge=-1.0, le=1.0, allow_inf_nan=False)
    evidence: list[AssistantEvidenceChunkResponse] = Field(max_length=5)


class AssistantMessageResponse(BaseModel):
    conversation_id: UUID
    message: AssistantMessageResponseMessage


class AssistantTurnCancellationResponse(BaseModel):
    outcome: Literal["completed", "cancelled", "uncertain"]
    result: AssistantMessageResponse | None = None
