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


class AssistantMessageResponseMessage(BaseModel):
    role: Literal["assistant"] = "assistant"
    content: str
    evidence: list["AssistantEvidenceProjectResponse"] = Field(default_factory=list)


class AssistantEvidenceChunkResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    chunk_index: int = Field(ge=0)
    chunk_text: str = Field(min_length=1, max_length=4000)


class AssistantEvidenceProjectResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    repo_id: int = Field(gt=0)
    full_name: str = Field(min_length=1, max_length=200)
    html_url: str = Field(min_length=1, max_length=500)
    evidence: list[AssistantEvidenceChunkResponse] = Field(max_length=5)


class AssistantMessageResponse(BaseModel):
    conversation_id: UUID
    message: AssistantMessageResponseMessage
