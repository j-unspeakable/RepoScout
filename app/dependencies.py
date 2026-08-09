from typing import Annotated

from fastapi import Depends, HTTPException, Request, status

from app.config import Settings
from app.services.corpus import CorpusService
from app.services.indexing_requests import IndexingRequestService
from app.services.ingestion import IngestionService
from app.services.project_tools import ProjectToolsService
from app.services.rag import RagService
from app.services.retrieval import RetrievalService
from app.services.supervisor import AssistantService


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


async def get_corpus_service(request: Request) -> CorpusService:
    service: CorpusService | None = getattr(request.app.state, "corpus_service", None)
    if service is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Corpus dependencies are unavailable",
        )
    return service


async def get_indexing_request_service(request: Request) -> IndexingRequestService:
    service: IndexingRequestService | None = getattr(
        request.app.state, "indexing_request_service", None
    )
    if service is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Indexing request dependencies are unavailable",
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


async def get_project_tools_service(request: Request) -> ProjectToolsService:
    service: ProjectToolsService | None = getattr(request.app.state, "project_tools_service", None)
    if service is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Project tool dependencies are unavailable",
        )
    return service


async def get_assistant_service(request: Request) -> AssistantService:
    service: AssistantService | None = getattr(request.app.state, "assistant_service", None)
    if service is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Ask RepoScout is not configured",
        )
    return service


async def get_project_user_key() -> str:
    """Return the capstone user scope from one replaceable identity boundary."""
    return "default"


SettingsDep = Annotated[Settings, Depends(get_app_settings)]
CorpusServiceDep = Annotated[CorpusService, Depends(get_corpus_service)]
IndexingRequestServiceDep = Annotated[IndexingRequestService, Depends(get_indexing_request_service)]
IngestionServiceDep = Annotated[IngestionService, Depends(get_ingestion_service)]
RetrievalServiceDep = Annotated[RetrievalService, Depends(get_retrieval_service)]
RagServiceDep = Annotated[RagService, Depends(get_rag_service)]
ProjectToolsServiceDep = Annotated[ProjectToolsService, Depends(get_project_tools_service)]
ProjectUserKeyDep = Annotated[str, Depends(get_project_user_key)]
AssistantServiceDep = Annotated[AssistantService, Depends(get_assistant_service)]
