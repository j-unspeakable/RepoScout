from dataclasses import dataclass

from app.repositories.search import (
    SearchCandidate,
    SearchRepositoryError,
    SearchRepositoryProtocol,
)
from app.services.embeddings import (
    EMBEDDING_MODEL_NAME,
    EmbeddingServiceError,
    EmbeddingServiceProtocol,
)


class RetrievalServiceError(RuntimeError):
    pass


class RetrievalDatabaseFailure(RetrievalServiceError):
    pass


class RetrievalEmbeddingFailure(RetrievalServiceError):
    pass


@dataclass(frozen=True, slots=True)
class EvidenceChunk:
    chunk_id: str
    chunk_index: int
    chunk_text: str
    similarity: float


@dataclass(frozen=True, slots=True)
class ProjectSearchResult:
    rank: int
    repo_id: int
    name: str
    full_name: str
    owner: str
    description: str | None
    html_url: str
    primary_language: str | None
    stars: int
    forks: int
    open_issues: int
    topics: list[str]
    license: str | None
    similarity: float
    evidence: list[EvidenceChunk]


@dataclass(frozen=True, slots=True)
class SemanticSearchResult:
    query: str
    embedding_model: str
    projects: list[ProjectSearchResult]


class RetrievalService:
    def __init__(
        self,
        embeddings: EmbeddingServiceProtocol,
        repository: SearchRepositoryProtocol,
        minimum_similarity: float,
    ) -> None:
        self._embeddings = embeddings
        self._repository = repository
        self._minimum_similarity = minimum_similarity

    async def search(
        self,
        query: str,
        top_k: int,
        language: str | None = None,
        minimum_stars: int | None = None,
    ) -> SemanticSearchResult:
        try:
            embedding = await self._embeddings.embed_query(query)
        except EmbeddingServiceError as exc:
            raise RetrievalEmbeddingFailure("Search embedding unavailable") from exc

        candidate_limit = min(200, max(20, top_k * 10))
        try:
            candidates = await self._repository.search_chunks(
                embedding,
                EMBEDDING_MODEL_NAME,
                language,
                minimum_stars,
                candidate_limit,
            )
        except SearchRepositoryError as exc:
            raise RetrievalDatabaseFailure("Search database unavailable") from exc

        projects = self._rank_projects(candidates, top_k)
        return SemanticSearchResult(
            query=query,
            embedding_model=EMBEDDING_MODEL_NAME,
            projects=projects,
        )

    def _rank_projects(
        self,
        candidates: list[SearchCandidate],
        top_k: int,
    ) -> list[ProjectSearchResult]:
        grouped: dict[int, list[tuple[SearchCandidate, float]]] = {}
        for candidate in candidates:
            similarity = max(-1.0, min(1.0, 1.0 - candidate.cosine_distance))
            if similarity < self._minimum_similarity:
                continue
            grouped.setdefault(candidate.repo_id, []).append((candidate, similarity))

        unranked: list[tuple[SearchCandidate, list[EvidenceChunk], float, float]] = []
        for matches in grouped.values():
            matches.sort(key=lambda item: (-item[1], item[0].chunk_index, item[0].chunk_id))
            best_candidate, best_similarity = matches[0]
            evidence = [
                EvidenceChunk(
                    chunk_id=candidate.chunk_id,
                    chunk_index=candidate.chunk_index,
                    chunk_text=candidate.chunk_text,
                    similarity=similarity,
                )
                for candidate, similarity in matches[:2]
            ]
            second_similarity = evidence[1].similarity if len(evidence) > 1 else -1.0
            unranked.append((best_candidate, evidence, best_similarity, second_similarity))

        unranked.sort(key=lambda item: (-item[2], -item[3], -item[0].stars, item[0].repo_id))
        return [
            ProjectSearchResult(
                rank=rank,
                repo_id=candidate.repo_id,
                name=candidate.name,
                full_name=candidate.full_name,
                owner=candidate.owner,
                description=candidate.description,
                html_url=candidate.html_url,
                primary_language=candidate.primary_language,
                stars=candidate.stars,
                forks=candidate.forks,
                open_issues=candidate.open_issues,
                topics=candidate.topics,
                license=candidate.license,
                similarity=best_similarity,
                evidence=evidence,
            )
            for rank, (candidate, evidence, best_similarity, _) in enumerate(
                unranked[:top_k], start=1
            )
        ]
