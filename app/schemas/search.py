from pydantic import BaseModel, ConfigDict, Field, field_validator


class SearchFilters(BaseModel):
    language: str | None = Field(default=None, min_length=1, max_length=100)
    minimum_stars: int | None = Field(default=None, ge=0)

    @field_validator("language")
    @classmethod
    def strip_language(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        if not stripped:
            raise ValueError("language must not be blank")
        return stripped


class SearchRequest(BaseModel):
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


class SearchResponseModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class EvidenceChunkResponse(SearchResponseModel):
    chunk_id: str
    chunk_index: int
    chunk_text: str
    similarity: float


class ProjectSearchResultResponse(SearchResponseModel):
    rank: int
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
    similarity: float
    evidence: list[EvidenceChunkResponse]


class SemanticSearchResponse(SearchResponseModel):
    query: str
    embedding_model: str
    projects: list[ProjectSearchResultResponse]


class AskSearchResponse(SearchResponseModel):
    query: str
    answer: str
    requested_model: str
    resolved_model: str | None
    projects: list[ProjectSearchResultResponse]
