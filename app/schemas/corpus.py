from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class NotSearchableReasonsResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    missing_readme: int = Field(ge=0)
    retrieval_error: int = Field(ge=0)
    awaiting_indexing: int = Field(ge=0)
    other: int = Field(ge=0)


class CorpusSummaryResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    repositories_ingested: int = Field(ge=0)
    readmes_available: int = Field(ge=0)
    repositories_searchable: int = Field(ge=0)
    searchable_chunks: int = Field(ge=0)
    repositories_not_searchable: int = Field(ge=0)
    not_searchable_reasons: NotSearchableReasonsResponse
    last_indexed_at: datetime | None
