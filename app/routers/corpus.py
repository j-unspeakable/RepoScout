from fastapi import APIRouter, HTTPException, status

from app.dependencies import CorpusServiceDep
from app.schemas.corpus import CorpusSummaryResponse
from app.services.corpus import CorpusUnavailableError

router = APIRouter(prefix="/corpus", tags=["corpus"])


@router.get("/summary")
async def corpus_summary(service: CorpusServiceDep) -> CorpusSummaryResponse:
    try:
        summary = await service.get_summary()
    except CorpusUnavailableError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc
    return CorpusSummaryResponse.model_validate(summary)
