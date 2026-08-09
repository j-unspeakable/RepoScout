from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field, field_validator


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


class AssistantMessageResponse(BaseModel):
    conversation_id: UUID
    message: AssistantMessageResponseMessage
