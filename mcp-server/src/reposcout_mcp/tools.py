from functools import lru_cache
from typing import Annotated, Any, Literal

from pydantic import AfterValidator, Field

from reposcout_mcp.client import RepoScoutClient, RepoScoutClientError
from reposcout_mcp.config import get_settings


def _strip_nonblank(value: str) -> str:
    stripped = value.strip()
    if not stripped:
        raise ValueError("value must not be blank")
    return stripped


QueryText = Annotated[
    str,
    Field(min_length=1, max_length=500),
    AfterValidator(_strip_nonblank),
]
LanguageText = Annotated[
    str,
    Field(min_length=1, max_length=100),
    AfterValidator(_strip_nonblank),
]
NoteText = Annotated[
    str,
    Field(min_length=1, max_length=2000),
    AfterValidator(_strip_nonblank),
]
RepositoryId = Annotated[int, Field(ge=1)]
TopK = Annotated[int, Field(ge=1, le=10)]
EvidenceLimit = Annotated[int, Field(ge=1, le=5)]
MinimumStars = Annotated[int, Field(ge=0)]
ProjectStatus = Literal["INTERESTED", "TO_TRY", "IN_PROGRESS", "COMPLETED"]


@lru_cache(maxsize=1)
def get_client() -> RepoScoutClient:
    return RepoScoutClient(get_settings())


def safe_call(operation: Any) -> dict[str, Any]:
    try:
        return operation()
    except RepoScoutClientError as exc:
        return {
            "error": {
                "code": exc.code,
                "message": str(exc),
                "retryable": exc.retryable,
            }
        }


def search_projects(
    query: QueryText,
    top_k: TopK = 5,
    language: LanguageText | None = None,
    minimum_stars: MinimumStars | None = None,
) -> dict[str, Any]:
    """Search RepoScout's indexed repositories using natural-language meaning.

    Use this to discover projects before calling the other tools. Results include
    stable repository IDs, GitHub metadata, ranking evidence, and match scores.
    """
    return safe_call(lambda: get_client().search_projects(query, top_k, language, minimum_stars))


def get_project_details(
    repo_id: RepositoryId,
    evidence_limit: EvidenceLimit = 3,
) -> dict[str, Any]:
    """Get one repository's metadata, bounded README evidence, status, and recent notes."""
    return safe_call(lambda: get_client().get_project_details(repo_id, evidence_limit))


def save_project(repo_id: RepositoryId) -> dict[str, Any]:
    """Save a repository without changing it when it has already been saved."""
    return safe_call(lambda: get_client().save_project(repo_id))


def update_project_status(
    repo_id: RepositoryId,
    status: ProjectStatus,
) -> dict[str, Any]:
    """Update an already-saved project to an accepted progress status."""
    return safe_call(lambda: get_client().update_project_status(repo_id, status))


def add_project_note(repo_id: RepositoryId, note: NoteText) -> dict[str, Any]:
    """Append a natural-language note to an already-saved repository."""
    return safe_call(lambda: get_client().add_project_note(repo_id, note))
