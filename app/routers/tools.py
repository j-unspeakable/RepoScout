from typing import Annotated, NoReturn

from fastapi import APIRouter, HTTPException, Path, Query, status

from app.dependencies import ProjectToolsServiceDep, ProjectUserKeyDep, RetrievalServiceDep
from app.schemas.tools import (
    ProjectDetailsResponse,
    ProjectNoteCreate,
    ProjectNoteResponse,
    ProjectStatusUpdate,
    SavedProjectResponse,
    ToolSearchRequest,
    ToolSearchResponse,
)
from app.services.project_tools import (
    ProjectNotFoundError,
    ProjectToolsUnavailableError,
    SavedProjectNotFoundError,
)
from app.services.retrieval import RetrievalDatabaseFailure, RetrievalEmbeddingFailure

router = APIRouter(prefix="/api/tools", tags=["tools"])
RepositoryId = Annotated[int, Path(ge=1)]


@router.post("/search-projects")
async def search_projects(
    request: ToolSearchRequest,
    service: RetrievalServiceDep,
) -> ToolSearchResponse:
    language = request.filters.language if request.filters else None
    minimum_stars = request.filters.minimum_stars if request.filters else None
    try:
        result = await service.search(request.query, request.top_k, language, minimum_stars)
    except (RetrievalDatabaseFailure, RetrievalEmbeddingFailure) as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc
    return ToolSearchResponse(query=result.query, projects=result.projects)


@router.get("/projects/{repo_id}")
async def get_project_details(
    repo_id: RepositoryId,
    service: ProjectToolsServiceDep,
    user_key: ProjectUserKeyDep,
    evidence_limit: Annotated[int, Query(ge=1, le=5)] = 3,
) -> ProjectDetailsResponse:
    try:
        record = await service.get_project_details(user_key, repo_id, evidence_limit)
    except (ProjectNotFoundError, SavedProjectNotFoundError) as exc:
        _raise_not_found(exc)
    except ProjectToolsUnavailableError as exc:
        _raise_unavailable(exc)
    return ProjectDetailsResponse.model_validate(record)


@router.put("/saved-projects/{repo_id}")
async def save_project(
    repo_id: RepositoryId,
    service: ProjectToolsServiceDep,
    user_key: ProjectUserKeyDep,
) -> SavedProjectResponse:
    try:
        record = await service.save_project(user_key, repo_id)
    except ProjectNotFoundError as exc:
        _raise_not_found(exc)
    except ProjectToolsUnavailableError as exc:
        _raise_unavailable(exc)
    return SavedProjectResponse.model_validate(record)


@router.patch("/saved-projects/{repo_id}/status")
async def update_project_status(
    repo_id: RepositoryId,
    request: ProjectStatusUpdate,
    service: ProjectToolsServiceDep,
    user_key: ProjectUserKeyDep,
) -> SavedProjectResponse:
    try:
        record = await service.update_project_status(user_key, repo_id, request.status)
    except SavedProjectNotFoundError as exc:
        _raise_not_found(exc)
    except ProjectToolsUnavailableError as exc:
        _raise_unavailable(exc)
    return SavedProjectResponse.model_validate(record)


@router.post("/saved-projects/{repo_id}/notes", status_code=status.HTTP_201_CREATED)
async def add_project_note(
    repo_id: RepositoryId,
    request: ProjectNoteCreate,
    service: ProjectToolsServiceDep,
    user_key: ProjectUserKeyDep,
) -> ProjectNoteResponse:
    try:
        record = await service.add_project_note(user_key, repo_id, request.note)
    except SavedProjectNotFoundError as exc:
        _raise_not_found(exc)
    except ProjectToolsUnavailableError as exc:
        _raise_unavailable(exc)
    return ProjectNoteResponse.model_validate(record)


def _raise_not_found(exc: Exception) -> NoReturn:
    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc


def _raise_unavailable(exc: Exception) -> NoReturn:
    raise HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail=str(exc),
    ) from exc
