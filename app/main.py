from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.config import AppEnvironment, Settings, get_settings
from app.database.credentials import LakebaseCredentialProvider
from app.database.pool import LakebasePool
from app.repositories.ingestion import IngestionRepository
from app.routers import health, ingestion
from app.services.github import GitHubService
from app.services.ingestion import IngestionService


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
    try:
        await database.open()
        repository = IngestionRepository(database)
        application.state.ingestion_service = IngestionService(github, repository)
        yield
    finally:
        application.state.ingestion_service = None
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
    application.include_router(health.router)
    application.include_router(ingestion.router)
    return application


app = create_app()
