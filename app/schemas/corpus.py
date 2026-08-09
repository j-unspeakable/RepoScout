from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class CorpusSummaryResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    repositories_ingested: int = Field(ge=0)
    readmes_available: int = Field(ge=0)
    repositories_searchable: int = Field(ge=0)
    searchable_chunks: int = Field(ge=0)
    last_indexed_at: datetime | None
