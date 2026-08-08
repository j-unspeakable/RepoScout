from typing import NoReturn

from fastapi import APIRouter, HTTPException, status

from app.dependencies import RagServiceDep, RetrievalServiceDep
from app.schemas.search import AskSearchResponse, SearchRequest, SemanticSearchResponse
from app.services.openrouter import (
    OpenRouterBadGateway,
    OpenRouterServiceUnavailable,
    OpenRouterTimeout,
)
from app.services.rag import GenerationUnavailableError
from app.services.retrieval import RetrievalDatabaseFailure, RetrievalEmbeddingFailure

router = APIRouter(prefix="/search", tags=["search"])


@router.post("/semantic")
async def semantic_search(
    request: SearchRequest,
    service: RetrievalServiceDep,
) -> SemanticSearchResponse:
    language = request.filters.language if request.filters else None
    minimum_stars = request.filters.minimum_stars if request.filters else None
    try:
        result = await service.search(
            request.query,
            request.top_k,
            language,
            minimum_stars,
        )
    except (RetrievalDatabaseFailure, RetrievalEmbeddingFailure) as exc:
        _raise_retrieval_error(exc)
    return SemanticSearchResponse.model_validate(result)


@router.post("/ask")
async def ask_search(
    request: SearchRequest,
    service: RagServiceDep,
) -> AskSearchResponse:
    language = request.filters.language if request.filters else None
    minimum_stars = request.filters.minimum_stars if request.filters else None
    try:
        result = await service.ask(
            request.query,
            request.top_k,
            language,
            minimum_stars,
        )
    except (RetrievalDatabaseFailure, RetrievalEmbeddingFailure) as exc:
        _raise_retrieval_error(exc)
    except GenerationUnavailableError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc
    except OpenRouterServiceUnavailable as exc:
        headers = {"Retry-After": str(exc.retry_after)} if exc.retry_after else None
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
            headers=headers,
        ) from exc
    except OpenRouterTimeout as exc:
        raise HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            detail=str(exc),
        ) from exc
    except OpenRouterBadGateway as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=str(exc),
        ) from exc
    return AskSearchResponse.model_validate(result)


def _raise_retrieval_error(exc: Exception) -> NoReturn:
    raise HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail=str(exc),
    ) from exc
