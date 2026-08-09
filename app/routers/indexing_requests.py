from fastapi import APIRouter, HTTPException, status

from app.dependencies import IndexingRequestServiceDep
from app.schemas.indexing_requests import IndexingRequestCreate, IndexingRequestResponse
from app.services.indexing_requests import IndexingRequestUnavailableError

router = APIRouter(prefix="/indexing-requests", tags=["indexing-requests"])


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_indexing_request(
    request: IndexingRequestCreate,
    service: IndexingRequestServiceDep,
) -> IndexingRequestResponse:
    try:
        record = await service.create_request(request.search_query, request.notes)
    except IndexingRequestUnavailableError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc
    return IndexingRequestResponse.model_validate(record)
