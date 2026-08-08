from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, datetime
from uuid import UUID

import pytest

from app.repositories.ingestion import IngestionRunRecord
from app.schemas.ingestion import IngestionStatus
from app.services.github import (
    GitHubSearchError,
    ReadmeData,
    ReadmeRetrievalStatus,
    RepositoryData,
    RepositoryWithReadme,
)
from app.services.ingestion import IngestionSearchFailure, IngestionService


def _repository(repo_id: int = 1, stars: int = 1) -> RepositoryData:
    now = datetime.now(UTC)
    return RepositoryData(
        repo_id=repo_id,
        name="repo",
        full_name="owner/repo",
        owner="owner",
        description=None,
        html_url="https://github.com/owner/repo",
        primary_language="Python",
        stars=stars,
        forks=0,
        open_issues=0,
        topics=[],
        license=None,
        created_at=now,
        updated_at=now,
        pushed_at=now,
        ingested_at=now,
    )


def _readme(
    status: ReadmeRetrievalStatus,
    content: str | None = None,
) -> ReadmeData:
    return ReadmeData(
        repo_id=1,
        raw_content=content,
        content_hash="hash" if content else None,
        retrieved_at=datetime.now(UTC),
        retrieval_status=status,
    )


class FakeGitHub:
    def __init__(self, repository: RepositoryData, readme: ReadmeData) -> None:
        self.repository = repository
        self.readme = readme
        self.search_error: Exception | None = None

    async def search_repositories(
        self, search_query: str, max_repositories: int
    ) -> list[RepositoryData]:
        if self.search_error:
            raise self.search_error
        return [self.repository][:max_repositories]

    async def retrieve_readmes(
        self, repositories: list[RepositoryData]
    ) -> list[RepositoryWithReadme]:
        return [RepositoryWithReadme(repository=repositories[0], readme=self.readme)]


class InMemoryRepository:
    def __init__(self) -> None:
        self.runs: dict[UUID, IngestionRunRecord] = {}
        self.repositories: dict[int, RepositoryData] = {}
        self.readmes: dict[int, ReadmeData] = {}

    async def create_run(
        self, run_id: UUID, search_query: str, started_at: datetime
    ) -> IngestionRunRecord:
        record = IngestionRunRecord(
            run_id=run_id,
            search_query=search_query,
            started_at=started_at,
            completed_at=None,
            repositories_found=0,
            repositories_inserted=0,
            repositories_updated=0,
            status=IngestionStatus.RUNNING,
            error_message=None,
        )
        self.runs[run_id] = record
        return record

    async def persist_completed_run(
        self,
        run_id: UUID,
        repositories: Sequence[RepositoryWithReadme],
        completed_at: datetime,
    ) -> IngestionRunRecord:
        existing_ids = set(self.repositories)
        for item in repositories:
            self.repositories[item.repository.repo_id] = item.repository
            previous = self.readmes.get(item.readme.repo_id)
            if item.readme.retrieval_status is ReadmeRetrievalStatus.ERROR and previous:
                self.readmes[item.readme.repo_id] = replace(
                    item.readme,
                    raw_content=previous.raw_content,
                    content_hash=previous.content_hash,
                )
            else:
                self.readmes[item.readme.repo_id] = item.readme

        inserted = sum(item.repository.repo_id not in existing_ids for item in repositories)
        record = replace(
            self.runs[run_id],
            completed_at=completed_at,
            repositories_found=len(repositories),
            repositories_inserted=inserted,
            repositories_updated=len(repositories) - inserted,
            status=IngestionStatus.COMPLETED,
        )
        self.runs[run_id] = record
        return record

    async def mark_run_failed(
        self, run_id: UUID, completed_at: datetime, error_message: str
    ) -> IngestionRunRecord | None:
        record = replace(
            self.runs[run_id],
            completed_at=completed_at,
            status=IngestionStatus.FAILED,
            error_message=error_message,
        )
        self.runs[run_id] = record
        return record

    async def get_run(self, run_id: UUID) -> IngestionRunRecord | None:
        return self.runs.get(run_id)


@pytest.mark.asyncio
async def test_repeated_ingestion_is_idempotent_and_refreshes_metadata() -> None:
    repository_store = InMemoryRepository()
    github = FakeGitHub(_repository(stars=1), _readme(ReadmeRetrievalStatus.AVAILABLE, "first"))
    service = IngestionService(github, repository_store)

    first = await service.ingest("fastapi", 30)
    github.repository = _repository(stars=2)
    github.readme = _readme(ReadmeRetrievalStatus.AVAILABLE, "second")
    second = await service.ingest("fastapi", 30)

    assert first.repositories_inserted == 1
    assert second.repositories_inserted == 0
    assert second.repositories_updated == 1
    assert len(repository_store.repositories) == 1
    assert repository_store.repositories[1].stars == 2
    assert repository_store.readmes[1].raw_content == "second"


@pytest.mark.asyncio
async def test_readme_error_continues_run_and_preserves_previous_content() -> None:
    repository_store = InMemoryRepository()
    github = FakeGitHub(_repository(), _readme(ReadmeRetrievalStatus.AVAILABLE, "known-good"))
    service = IngestionService(github, repository_store)
    await service.ingest("fastapi", 30)

    github.readme = _readme(ReadmeRetrievalStatus.ERROR)
    run = await service.ingest("fastapi", 30)

    assert run.status is IngestionStatus.COMPLETED
    assert repository_store.readmes[1].retrieval_status is ReadmeRetrievalStatus.ERROR
    assert repository_store.readmes[1].raw_content == "known-good"


@pytest.mark.asyncio
async def test_missing_readme_clears_previous_content() -> None:
    repository_store = InMemoryRepository()
    github = FakeGitHub(_repository(), _readme(ReadmeRetrievalStatus.AVAILABLE, "known-good"))
    service = IngestionService(github, repository_store)
    await service.ingest("fastapi", 30)

    github.readme = _readme(ReadmeRetrievalStatus.MISSING)
    await service.ingest("fastapi", 30)

    assert repository_store.readmes[1].retrieval_status is ReadmeRetrievalStatus.MISSING
    assert repository_store.readmes[1].raw_content is None


@pytest.mark.asyncio
async def test_repository_search_failure_marks_run_failed() -> None:
    repository_store = InMemoryRepository()
    github = FakeGitHub(_repository(), _readme(ReadmeRetrievalStatus.MISSING))
    github.search_error = GitHubSearchError("safe search failure")
    service = IngestionService(github, repository_store)

    with pytest.raises(IngestionSearchFailure) as error:
        await service.ingest("fastapi", 30)

    run = repository_store.runs[error.value.run_id]
    assert run.status is IngestionStatus.FAILED
    assert run.error_message == "safe search failure"
