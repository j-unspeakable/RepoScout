from typing import Annotated

from fastapi import Depends, HTTPException, Request, status

from app.config import Settings
from app.services.ingestion import IngestionService
from app.services.rag import RagService
from app.services.retrieval import RetrievalService


async def get_app_settings(request: Request) -> Settings:
    return request.app.state.settings


async def get_ingestion_service(request: Request) -> IngestionService:
    service: IngestionService | None = getattr(request.app.state, "ingestion_service", None)
    if service is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Ingestion dependencies are unavailable",
        )
    return service


async def get_retrieval_service(request: Request) -> RetrievalService:
    service: RetrievalService | None = getattr(request.app.state, "retrieval_service", None)
    if service is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Search dependencies are unavailable",
        )
    return service


async def get_rag_service(request: Request) -> RagService:
    service: RagService | None = getattr(request.app.state, "rag_service", None)
    if service is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="RAG dependencies are unavailable",
        )
    return service


SettingsDep = Annotated[Settings, Depends(get_app_settings)]
IngestionServiceDep = Annotated[IngestionService, Depends(get_ingestion_service)]
RetrievalServiceDep = Annotated[RetrievalService, Depends(get_retrieval_service)]
RagServiceDep = Annotated[RagService, Depends(get_rag_service)]
