from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.config import AppEnvironment, Settings, get_settings
from app.database.credentials import LakebaseCredentialProvider
from app.database.pool import LakebasePool
from app.repositories.ingestion import IngestionRepository
from app.repositories.search import SearchRepository
from app.routers import health, ingestion, search
from app.services.embeddings import SentenceTransformerEmbeddingService
from app.services.github import GitHubService
from app.services.ingestion import IngestionService
from app.services.openrouter import OpenRouterClient
from app.services.rag import RagService
from app.services.retrieval import RetrievalService


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
        search_repository = SearchRepository(database)
        embeddings = SentenceTransformerEmbeddingService()
        retrieval = RetrievalService(
            embeddings,
            search_repository,
            settings.search_min_similarity,
        )
        application.state.ingestion_service = IngestionService(github, ingestion_repository)
        application.state.retrieval_service = retrieval
        application.state.rag_service = RagService(
            retrieval,
            openrouter,
            settings.llm_model_name,
        )
        yield
    finally:
        application.state.ingestion_service = None
        application.state.retrieval_service = None
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
    application.state.retrieval_service = None
    application.state.rag_service = None
    application.include_router(health.router)
    application.include_router(ingestion.router)
    application.include_router(search.router)
    return application


app = create_app()
