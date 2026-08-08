from datetime import UTC, datetime
from uuid import UUID, uuid4

from app.repositories.ingestion import (
    IngestionRepositoryError,
    IngestionRepositoryProtocol,
    IngestionRunRecord,
)
from app.services.github import (
    GitHubRateLimitError,
    GitHubSearchError,
    GitHubServiceProtocol,
)


class IngestionServiceError(RuntimeError):
    def __init__(self, message: str, run_id: UUID) -> None:
        super().__init__(message)
        self.run_id = run_id


class IngestionRateLimitFailure(IngestionServiceError):
    def __init__(self, message: str, run_id: UUID, retry_after: int | None) -> None:
        super().__init__(message, run_id)
        self.retry_after = retry_after


class IngestionSearchFailure(IngestionServiceError):
    pass


class IngestionDatabaseFailure(IngestionServiceError):
    pass


class IngestionRunFailure(IngestionServiceError):
    pass


class IngestionService:
    def __init__(
        self,
        github: GitHubServiceProtocol,
        repository: IngestionRepositoryProtocol,
    ) -> None:
        self._github = github
        self._repository = repository

    async def ingest(self, search_query: str, max_repositories: int) -> IngestionRunRecord:
        run_id = uuid4()
        started_at = datetime.now(UTC)
        try:
            await self._repository.create_run(run_id, search_query, started_at)
        except IngestionRepositoryError as exc:
            raise IngestionDatabaseFailure("Database unavailable", run_id) from exc

        try:
            repositories = await self._github.search_repositories(search_query, max_repositories)
        except GitHubRateLimitError as exc:
            await self._record_failure(run_id, str(exc))
            raise IngestionRateLimitFailure(
                "GitHub rate limit exceeded", run_id, exc.retry_after
            ) from exc
        except GitHubSearchError as exc:
            await self._record_failure(run_id, str(exc))
            raise IngestionSearchFailure("GitHub repository search failed", run_id) from exc

        try:
            repositories_with_readmes = await self._github.retrieve_readmes(repositories)
        except Exception as exc:
            await self._record_failure(run_id, "Ingestion orchestration failed")
            raise IngestionRunFailure("Ingestion run failed", run_id) from exc
        try:
            return await self._repository.persist_completed_run(
                run_id,
                repositories_with_readmes,
                datetime.now(UTC),
            )
        except IngestionRepositoryError as exc:
            await self._record_failure(run_id, "Database persistence failed")
            raise IngestionDatabaseFailure("Database unavailable", run_id) from exc

    async def get_run(self, run_id: UUID) -> IngestionRunRecord | None:
        try:
            return await self._repository.get_run(run_id)
        except IngestionRepositoryError as exc:
            raise IngestionDatabaseFailure("Database unavailable", run_id) from exc

    async def _record_failure(self, run_id: UUID, message: str) -> None:
        try:
            await self._repository.mark_run_failed(run_id, datetime.now(UTC), message)
        except IngestionRepositoryError as exc:
            raise IngestionDatabaseFailure("Database unavailable", run_id) from exc
