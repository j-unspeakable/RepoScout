from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, HTTPException, Path, status

from app.dependencies import IngestionServiceDep
from app.schemas.ingestion import IngestionRequest, IngestionRunResponse
from app.services.ingestion import (
    IngestionDatabaseFailure,
    IngestionRateLimitFailure,
    IngestionRunFailure,
    IngestionSearchFailure,
)

router = APIRouter(prefix="/ingestions", tags=["ingestion"])


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_ingestion(
    ingestion: IngestionRequest,
    service: IngestionServiceDep,
) -> IngestionRunResponse:
    try:
        run = await service.ingest(
            ingestion.search_query,
            ingestion.max_repositories,
        )
    except IngestionRateLimitFailure as exc:
        headers = {"Retry-After": str(exc.retry_after)} if exc.retry_after else None
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"message": str(exc), "run_id": str(exc.run_id)},
            headers=headers,
        ) from exc
    except IngestionSearchFailure as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={"message": str(exc), "run_id": str(exc.run_id)},
        ) from exc
    except IngestionDatabaseFailure as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"message": str(exc), "run_id": str(exc.run_id)},
        ) from exc
    except IngestionRunFailure as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"message": str(exc), "run_id": str(exc.run_id)},
        ) from exc

    return IngestionRunResponse.model_validate(run, from_attributes=True)


@router.get("/{run_id}")
async def get_ingestion(
    run_id: Annotated[UUID, Path(description="The ingestion run identifier")],
    service: IngestionServiceDep,
) -> IngestionRunResponse:
    try:
        run = await service.get_run(run_id)
    except IngestionDatabaseFailure as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"message": str(exc), "run_id": str(exc.run_id)},
        ) from exc
    if run is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Ingestion run not found",
        )
    return IngestionRunResponse.model_validate(run, from_attributes=True)
