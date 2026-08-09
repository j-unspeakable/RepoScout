from fastapi import APIRouter, HTTPException, status

from app.dependencies import ProjectToolsServiceDep, ProjectUserKeyDep
from app.schemas.projects import SavedProjectListItem, SavedProjectsResponse
from app.services.project_tools import ProjectToolsUnavailableError

router = APIRouter(prefix="/saved-projects", tags=["saved projects"])


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
