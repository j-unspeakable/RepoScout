from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any, cast

import httpx
import psycopg
import pytest

from app.config import AppEnvironment, Settings
from app.database.pool import ConnectionProvider
from app.dependencies import get_corpus_service
from app.main import create_app
from app.repositories.corpus import (
    CorpusRepository,
    CorpusRepositoryError,
    CorpusSummaryRecord,
)
from app.services.corpus import CorpusService, CorpusUnavailableError


class FakeCursor:
    def __init__(self, row: dict[str, Any] | None) -> None:
        self.row = row
        self.executed_sql = ""

    async def __aenter__(self) -> "FakeCursor":
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    async def execute(self, query: str) -> None:
        self.executed_sql = query

    async def fetchone(self) -> dict[str, Any] | None:
        return self.row


class FakeConnection:
    def __init__(self, cursor: FakeCursor) -> None:
        self._cursor = cursor

    def cursor(self, **kwargs: object) -> FakeCursor:
        return self._cursor


class FakeDatabase:
    def __init__(
        self,
        cursor: FakeCursor | None = None,
        error: psycopg.Error | None = None,
    ) -> None:
        self.cursor = cursor
        self.error = error

    @asynccontextmanager
    async def connection(self) -> AsyncIterator[FakeConnection]:
        if self.error is not None:
            raise self.error
        assert self.cursor is not None
        yield FakeConnection(self.cursor)


class FakeCorpusRepository:
    def __init__(
        self,
        record: CorpusSummaryRecord | None = None,
        error: Exception | None = None,
    ) -> None:
        self.record = record
        self.error = error

    async def get_summary(self) -> CorpusSummaryRecord:
        if self.error is not None:
            raise self.error
        assert self.record is not None
        return self.record


class FakeCorpusService:
    def __init__(
        self,
        record: CorpusSummaryRecord | None = None,
        error: Exception | None = None,
    ) -> None:
        self.record = record
        self.error = error

    async def get_summary(self) -> CorpusSummaryRecord:
        if self.error is not None:
            raise self.error
        assert self.record is not None
        return self.record


def _record(last_indexed_at: datetime | None = None) -> CorpusSummaryRecord:
    return CorpusSummaryRecord(
        repositories_ingested=269,
        readmes_available=265,
        repositories_searchable=215,
        searchable_chunks=989,
        last_indexed_at=last_indexed_at,
    )


@pytest.mark.asyncio
async def test_corpus_repository_returns_real_counts_and_latest_index_time() -> None:
    indexed_at = datetime(2026, 8, 8, 17, 41, tzinfo=UTC)
    cursor = FakeCursor(
        {
            "repositories_ingested": 269,
            "readmes_available": 265,
            "repositories_searchable": 215,
            "searchable_chunks": 989,
            "last_indexed_at": indexed_at,
        }
    )
    repository = CorpusRepository(cast(ConnectionProvider, FakeDatabase(cursor)))

    result = await repository.get_summary()

    assert result == _record(indexed_at)
    assert cursor.executed_sql.count("SELECT count(*) FROM repositories") == 1
    assert "retrieval_status = 'available'" in cursor.executed_sql
    assert "count(DISTINCT repo_id) FROM repository_chunks" in cursor.executed_sql
    assert "max(processed_at) FROM repository_chunks" in cursor.executed_sql


@pytest.mark.asyncio
async def test_corpus_repository_supports_empty_index_and_maps_database_errors() -> None:
    cursor = FakeCursor(
        {
            "repositories_ingested": 0,
            "readmes_available": 0,
            "repositories_searchable": 0,
            "searchable_chunks": 0,
            "last_indexed_at": None,
        }
    )
    repository = CorpusRepository(cast(ConnectionProvider, FakeDatabase(cursor)))

    result = await repository.get_summary()

    assert result == CorpusSummaryRecord(0, 0, 0, 0, None)

    unavailable = CorpusRepository(
        cast(
            ConnectionProvider,
            FakeDatabase(error=psycopg.OperationalError("database secret")),
        )
    )
    with pytest.raises(CorpusRepositoryError, match="Unable to retrieve corpus summary") as caught:
        await unavailable.get_summary()
    assert "database secret" not in str(caught.value)


@pytest.mark.asyncio
async def test_corpus_service_maps_repository_failure_safely() -> None:
    service = CorpusService(FakeCorpusRepository(error=CorpusRepositoryError("database secret")))

    with pytest.raises(CorpusUnavailableError, match="Corpus summary unavailable") as caught:
        await service.get_summary()

    assert "database secret" not in str(caught.value)


@asynccontextmanager
async def _client(
    service: FakeCorpusService | None = None,
) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(Settings(app_env=AppEnvironment.TEST))
    if service is not None:

        async def override_corpus_service() -> FakeCorpusService:
            return service

        app.dependency_overrides[get_corpus_service] = override_corpus_service
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            yield client


@pytest.mark.asyncio
async def test_corpus_summary_route_returns_typed_summary() -> None:
    indexed_at = datetime(2026, 8, 8, 17, 41, tzinfo=UTC)
    async with _client(FakeCorpusService(record=_record(indexed_at))) as client:
        response = await client.get("/corpus/summary")

    assert response.status_code == 200
    assert response.json() == {
        "repositories_ingested": 269,
        "readmes_available": 265,
        "repositories_searchable": 215,
        "searchable_chunks": 989,
        "last_indexed_at": "2026-08-08T17:41:00Z",
    }


@pytest.mark.asyncio
async def test_corpus_summary_route_maps_failure_and_missing_dependency() -> None:
    async with _client(
        FakeCorpusService(error=CorpusUnavailableError("Corpus summary unavailable"))
    ) as client:
        failure = await client.get("/corpus/summary")
    async with _client() as client:
        unavailable = await client.get("/corpus/summary")

    assert failure.status_code == 503
    assert failure.json() == {"detail": "Corpus summary unavailable"}
    assert unavailable.status_code == 503
    assert unavailable.json() == {"detail": "Corpus dependencies are unavailable"}
