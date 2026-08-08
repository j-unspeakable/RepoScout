from typing import Any

import pytest

from app.repositories.search import SearchCandidate, SearchRepositoryError
from app.services.embeddings import EMBEDDING_MODEL_NAME, EmbeddingServiceError
from app.services.retrieval import (
    RetrievalDatabaseFailure,
    RetrievalEmbeddingFailure,
    RetrievalService,
)


def _candidate(
    repo_id: int,
    similarity: float,
    *,
    chunk_index: int = 0,
    stars: int = 100,
) -> SearchCandidate:
    return SearchCandidate(
        chunk_id=f"chunk-{repo_id}-{chunk_index}",
        chunk_index=chunk_index,
        chunk_text=f"Evidence for repository {repo_id}",
        repo_id=repo_id,
        name=f"repo-{repo_id}",
        full_name=f"owner/repo-{repo_id}",
        owner="owner",
        description="Data engineering project",
        html_url=f"https://github.com/owner/repo-{repo_id}",
        primary_language="Python",
        stars=stars,
        forks=10,
        open_issues=2,
        topics=["data-engineering"],
        license="MIT",
        cosine_distance=1.0 - similarity,
    )


class FakeEmbeddings:
    error: Exception | None = None

    async def embed_query(self, query: str) -> list[float]:
        if self.error:
            raise self.error
        return [1.0] + [0.0] * 383


class FakeRepository:
    def __init__(self, candidates: list[SearchCandidate]) -> None:
        self.candidates = candidates
        self.calls: list[tuple[Any, ...]] = []
        self.error: Exception | None = None

    async def search_chunks(
        self,
        embedding: list[float],
        embedding_model: str,
        language: str | None,
        minimum_stars: int | None,
        candidate_limit: int,
    ) -> list[SearchCandidate]:
        self.calls.append((embedding, embedding_model, language, minimum_stars, candidate_limit))
        if self.error:
            raise self.error
        return self.candidates


@pytest.mark.asyncio
async def test_retrieval_keeps_above_threshold_candidates_and_forwards_filters() -> None:
    repository = FakeRepository([_candidate(1, 0.7), _candidate(2, 0.6)])
    service = RetrievalService(FakeEmbeddings(), repository, minimum_similarity=0.25)

    result = await service.search("pipelines", 5, "Python", 100)

    assert [project.repo_id for project in result.projects] == [1, 2]
    assert repository.calls[0][1:] == (EMBEDDING_MODEL_NAME, "Python", 100, 50)


@pytest.mark.asyncio
async def test_retrieval_removes_all_below_threshold_candidates() -> None:
    repository = FakeRepository([_candidate(1, 0.24), _candidate(2, -0.1)])
    service = RetrievalService(FakeEmbeddings(), repository, minimum_similarity=0.25)

    result = await service.search("unrelated", 5)

    assert result.projects == []


@pytest.mark.asyncio
async def test_retrieval_filters_mixed_candidates_before_grouping_and_caps_evidence() -> None:
    candidates = [
        _candidate(1, 0.80, chunk_index=2),
        _candidate(1, 0.70, chunk_index=1),
        _candidate(1, 0.60, chunk_index=0),
        _candidate(2, 0.24, stars=1000),
        _candidate(3, 0.65, stars=50),
    ]
    repository = FakeRepository(candidates)
    service = RetrievalService(FakeEmbeddings(), repository, minimum_similarity=0.25)

    result = await service.search("pipelines", 10)

    assert [project.repo_id for project in result.projects] == [1, 3]
    assert [item.chunk_index for item in result.projects[0].evidence] == [2, 1]
    assert len(result.projects[0].evidence) == 2
    assert result.projects[0].rank == 1


@pytest.mark.asyncio
async def test_retrieval_maps_embedding_and_database_failures() -> None:
    embeddings = FakeEmbeddings()
    embeddings.error = EmbeddingServiceError("unsafe internal detail")
    service = RetrievalService(embeddings, FakeRepository([]), 0.25)
    with pytest.raises(RetrievalEmbeddingFailure, match="embedding unavailable"):
        await service.search("query", 5)

    repository = FakeRepository([])
    repository.error = SearchRepositoryError("unsafe internal detail")
    service = RetrievalService(FakeEmbeddings(), repository, 0.25)
    with pytest.raises(RetrievalDatabaseFailure, match="database unavailable"):
        await service.search("query", 5)
