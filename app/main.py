from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI

from app.config import AppEnvironment, Settings, get_settings
from app.database.credentials import LakebaseCredentialProvider
from app.database.pool import LakebasePool
from app.repositories.corpus import CorpusRepository
from app.repositories.indexing_requests import IndexingRequestRepository
from app.repositories.ingestion import IngestionRepository
from app.repositories.project_tools import ProjectToolsRepository
from app.repositories.search import SearchRepository
from app.routers import corpus, health, indexing_requests, ingestion, search, tools
from app.services.corpus import CorpusService
from app.services.embeddings import SentenceTransformerEmbeddingService
from app.services.github import GitHubService
from app.services.indexing_requests import IndexingRequestService
from app.services.ingestion import IngestionService
from app.services.openrouter import OpenRouterClient
from app.services.project_tools import ProjectToolsService
from app.services.rag import RagService
from app.services.retrieval import RetrievalService

FRONTEND_DIRECTORY = Path(__file__).resolve().parent.parent / "frontend"


@asynccontextmanager
async def lifespan(application: FastAPI) -> AsyncIterator[None]:
    settings: Settings = application.state.settings
    if settings.app_env is AppEnvironment.TEST:
        yield
        return

    endpoint = settings.lakebase_endpoint
    if endpoint is None:  # Configuration validation should make this unreachable.
        raise RuntimeError("LAKEBASE_ENDPOINT is required")

    credential_provider = LakebaseCredentialProvider(
        endpoint,
        profile=settings.databricks_config_profile,
    )
    database = LakebasePool(settings, credential_provider)
    github = GitHubService(settings)
    openrouter = OpenRouterClient(settings) if settings.llm_api_key else None
    try:
        await database.open()
        ingestion_repository = IngestionRepository(database)
        corpus_repository = CorpusRepository(database)
        indexing_request_repository = IndexingRequestRepository(database)
        project_tools_repository = ProjectToolsRepository(database)
        search_repository = SearchRepository(database)
        embeddings = SentenceTransformerEmbeddingService()
        retrieval = RetrievalService(
            embeddings,
            search_repository,
            settings.search_min_similarity,
        )
        application.state.ingestion_service = IngestionService(github, ingestion_repository)
        application.state.corpus_service = CorpusService(corpus_repository)
        application.state.indexing_request_service = IndexingRequestService(
            indexing_request_repository
        )
        application.state.retrieval_service = retrieval
        application.state.project_tools_service = ProjectToolsService(project_tools_repository)
        application.state.rag_service = RagService(
            retrieval,
            openrouter,
            settings.llm_model_name,
        )
        yield
    finally:
        application.state.ingestion_service = None
        application.state.corpus_service = None
        application.state.indexing_request_service = None
        application.state.retrieval_service = None
        application.state.project_tools_service = None
        application.state.rag_service = None
        if openrouter is not None:
            await openrouter.close()
        await github.close()
        await database.close()


def create_app(settings: Settings | None = None) -> FastAPI:
    application = FastAPI(
        title="RepoScout",
        version="0.1.0",
        lifespan=lifespan,
    )
    application.state.settings = settings or get_settings()
    application.state.ingestion_service = None
    application.state.corpus_service = None
    application.state.indexing_request_service = None
    application.state.retrieval_service = None
    application.state.project_tools_service = None
    application.state.rag_service = None
    application.include_router(health.router)
    application.include_router(corpus.router)
    application.include_router(indexing_requests.router)
    application.include_router(ingestion.router)
    application.include_router(search.router)
    application.include_router(tools.router)
    application.frontend("/", directory=FRONTEND_DIRECTORY, fallback=None)
    return application


app = create_app()
