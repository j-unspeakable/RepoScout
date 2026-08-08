from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
import pytest

from app.config import AppEnvironment, Settings
from app.dependencies import get_rag_service, get_retrieval_service
from app.main import create_app
from app.services.openrouter import OpenRouterServiceUnavailable
from app.services.rag import AskResult
from app.services.retrieval import (
    EvidenceChunk,
    ProjectSearchResult,
    SemanticSearchResult,
)


def _project() -> ProjectSearchResult:
    return ProjectSearchResult(
        rank=1,
        repo_id=1,
        name="pipeline",
        full_name="owner/pipeline",
        owner="owner",
        description="Pipeline orchestration",
        html_url="https://github.com/owner/pipeline",
        primary_language="Python",
        stars=500,
        forks=20,
        open_issues=5,
        topics=["data-engineering"],
        license="MIT",
        similarity=0.72,
        evidence=[EvidenceChunk("chunk-1", 0, "Pipeline evidence", 0.72)],
    )


class FakeRetrievalService:
    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []

    async def search(
        self,
        query: str,
        top_k: int,
        language: str | None = None,
        minimum_stars: int | None = None,
    ) -> SemanticSearchResult:
        self.calls.append((query, top_k, language, minimum_stars))
        return SemanticSearchResult(
            query=query,
            embedding_model="sentence-transformers/all-MiniLM-L6-v2",
            projects=[_project()],
        )


class FakeRagService:
    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []
        self.error: Exception | None = None

    async def ask(
        self,
        query: str,
        top_k: int,
        language: str | None = None,
        minimum_stars: int | None = None,
    ) -> AskResult:
        self.calls.append((query, top_k, language, minimum_stars))
        if self.error:
            raise self.error
        return AskResult(
            query=query,
            answer="Use Pipeline [owner/pipeline#chunk-0].",
            requested_model="openrouter/free",
            resolved_model="provider/model:free",
            projects=[_project()],
        )


@asynccontextmanager
async def _client(
    retrieval: FakeRetrievalService | None = None,
    rag: FakeRagService | None = None,
) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(Settings(app_env=AppEnvironment.TEST))
    if retrieval is not None:

        async def override_retrieval() -> FakeRetrievalService:
            return retrieval

        app.dependency_overrides[get_retrieval_service] = override_retrieval
    if rag is not None:

        async def override_rag() -> FakeRagService:
            return rag

        app.dependency_overrides[get_rag_service] = override_rag
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            yield client


@pytest.mark.asyncio
async def test_semantic_search_returns_typed_ranked_projects_and_filters() -> None:
    retrieval = FakeRetrievalService()
    async with _client(retrieval=retrieval) as client:
        response = await client.post(
            "/search/semantic",
            json={
                "query": "  data pipelines  ",
                "top_k": 3,
                "filters": {"language": " Python ", "minimum_stars": 100},
            },
        )

    assert response.status_code == 200
    assert retrieval.calls == [("data pipelines", 3, "Python", 100)]
    assert response.json()["projects"][0]["evidence"][0]["similarity"] == 0.72


@pytest.mark.asyncio
async def test_ask_returns_answer_and_the_evidence_used() -> None:
    rag = FakeRagService()
    async with _client(rag=rag) as client:
        response = await client.post("/search/ask", json={"query": "scheduler"})

    assert response.status_code == 200
    assert response.json()["requested_model"] == "openrouter/free"
    assert response.json()["resolved_model"] == "provider/model:free"
    assert response.json()["projects"][0]["full_name"] == "owner/pipeline"


@pytest.mark.asyncio
async def test_search_request_validation_and_unavailable_dependency() -> None:
    retrieval = FakeRetrievalService()
    async with _client(retrieval=retrieval) as client:
        blank = await client.post("/search/semantic", json={"query": "  "})
        too_many = await client.post("/search/semantic", json={"query": "query", "top_k": 11})
    async with _client() as client:
        unavailable = await client.post("/search/semantic", json={"query": "query"})

    assert blank.status_code == 422
    assert too_many.status_code == 422
    assert unavailable.status_code == 503


@pytest.mark.asyncio
async def test_ask_preserves_safe_retry_after_for_generation_availability() -> None:
    rag = FakeRagService()
    rag.error = OpenRouterServiceUnavailable("Generation service unavailable", 9)
    async with _client(rag=rag) as client:
        response = await client.post("/search/ask", json={"query": "scheduler"})

    assert response.status_code == 503
    assert response.headers["Retry-After"] == "9"
    assert response.json()["detail"] == "Generation service unavailable"
