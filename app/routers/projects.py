from typing import Annotated

from fastapi import APIRouter, HTTPException, Path, Response, status

from app.dependencies import ProjectToolsServiceDep, ProjectUserKeyDep
from app.schemas.projects import SavedProjectListItem, SavedProjectsResponse
from app.services.project_tools import (
    ProjectToolsUnavailableError,
    SavedProjectNotFoundError,
)

router = APIRouter(prefix="/saved-projects", tags=["saved projects"])
RepositoryId = Annotated[int, Path(ge=1)]


@router.get("")
async def list_saved_projects(
    service: ProjectToolsServiceDep,
    user_key: ProjectUserKeyDep,
) -> SavedProjectsResponse:
    try:
        records = await service.list_saved_projects(user_key)
    except ProjectToolsUnavailableError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc
    return SavedProjectsResponse(
        projects=[SavedProjectListItem.model_validate(record) for record in records]
    )


@router.delete("/{repo_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_saved_project(
    repo_id: RepositoryId,
    service: ProjectToolsServiceDep,
    user_key: ProjectUserKeyDep,
) -> Response:
    try:
        await service.remove_saved_project(user_key, repo_id)
    except SavedProjectNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc
    except ProjectToolsUnavailableError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)
