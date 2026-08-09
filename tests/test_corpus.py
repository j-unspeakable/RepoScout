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
    NotSearchableReasonsRecord,
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
        repositories_not_searchable=54,
        not_searchable_reasons=NotSearchableReasonsRecord(
            missing_readme=4,
            retrieval_error=0,
            awaiting_indexing=49,
            other=1,
        ),
        last_indexed_at=last_indexed_at,
    )


def _summary_row(
    *,
    ingested: int,
    available: int,
    searchable: int,
    chunks: int,
    missing: int,
    retrieval_error: int,
    awaiting: int,
    other: int = 0,
    last_indexed_at: datetime | None = None,
) -> dict[str, Any]:
    return {
        "repositories_ingested": ingested,
        "readmes_available": available,
        "repositories_searchable": searchable,
        "searchable_chunks": chunks,
        "repositories_not_searchable": ingested - searchable,
        "missing_readme": missing,
        "retrieval_error": retrieval_error,
        "awaiting_indexing": awaiting,
        "other": other,
        "last_indexed_at": last_indexed_at,
    }


@pytest.mark.asyncio
async def test_corpus_repository_returns_real_counts_and_latest_index_time() -> None:
    indexed_at = datetime(2026, 8, 8, 17, 41, tzinfo=UTC)
    cursor = FakeCursor(
        _summary_row(
            ingested=269,
            available=265,
            searchable=215,
            chunks=989,
            missing=4,
            retrieval_error=0,
            awaiting=49,
            other=1,
            last_indexed_at=indexed_at,
        )
    )
    repository = CorpusRepository(cast(ConnectionProvider, FakeDatabase(cursor)))

    result = await repository.get_summary()

    assert result == _record(indexed_at)
    assert "WITH chunk_state AS" in cursor.executed_sql
    assert "LEFT JOIN repository_readmes" in cursor.executed_sql
    assert "count(*) FILTER (WHERE has_chunks)" in cursor.executed_sql
    assert "NOT has_chunks AND retrieval_status = 'missing'" in cursor.executed_sql
    assert "NOT has_chunks AND retrieval_status = 'error'" in cursor.executed_sql
    assert "NOT has_chunks AND retrieval_status = 'available'" in cursor.executed_sql
    assert "max(last_indexed_at)" in cursor.executed_sql


@pytest.mark.parametrize(
    ("row", "expected_searchable", "expected_reasons"),
    [
        (
            _summary_row(
                ingested=4,
                available=4,
                searchable=4,
                chunks=20,
                missing=0,
                retrieval_error=0,
                awaiting=0,
            ),
            4,
            NotSearchableReasonsRecord(0, 0, 0, 0),
        ),
        (
            _summary_row(
                ingested=1,
                available=0,
                searchable=0,
                chunks=0,
                missing=1,
                retrieval_error=0,
                awaiting=0,
            ),
            0,
            NotSearchableReasonsRecord(1, 0, 0, 0),
        ),
        (
            _summary_row(
                ingested=1,
                available=0,
                searchable=0,
                chunks=0,
                missing=0,
                retrieval_error=1,
                awaiting=0,
            ),
            0,
            NotSearchableReasonsRecord(0, 1, 0, 0),
        ),
        (
            _summary_row(
                ingested=1,
                available=0,
                searchable=1,
                chunks=3,
                missing=0,
                retrieval_error=0,
                awaiting=0,
            ),
            1,
            NotSearchableReasonsRecord(0, 0, 0, 0),
        ),
        (
            _summary_row(
                ingested=1,
                available=1,
                searchable=0,
                chunks=0,
                missing=0,
                retrieval_error=0,
                awaiting=1,
            ),
            0,
            NotSearchableReasonsRecord(0, 0, 1, 0),
        ),
        (
            _summary_row(
                ingested=10,
                available=6,
                searchable=4,
                chunks=30,
                missing=2,
                retrieval_error=1,
                awaiting=2,
                other=1,
            ),
            4,
            NotSearchableReasonsRecord(2, 1, 2, 1),
        ),
    ],
    ids=(
        "all-searchable",
        "missing-readme",
        "retrieval-error-without-chunks",
        "retrieval-error-with-existing-chunks",
        "available-awaiting-indexing",
        "mixed-reasons",
    ),
)
def test_corpus_reason_classification_contract(
    row: dict[str, Any],
    expected_searchable: int,
    expected_reasons: NotSearchableReasonsRecord,
) -> None:
    result = CorpusRepository._record_from_row(row)

    assert result.repositories_searchable == expected_searchable
    assert result.not_searchable_reasons == expected_reasons
    assert result.repositories_not_searchable == result.repositories_ingested - expected_searchable


@pytest.mark.asyncio
async def test_corpus_repository_supports_empty_index_and_maps_database_errors() -> None:
    cursor = FakeCursor(
        _summary_row(
            ingested=0,
            available=0,
            searchable=0,
            chunks=0,
            missing=0,
            retrieval_error=0,
            awaiting=0,
        )
    )
    repository = CorpusRepository(cast(ConnectionProvider, FakeDatabase(cursor)))

    result = await repository.get_summary()

    assert result == CorpusSummaryRecord(
        0,
        0,
        0,
        0,
        0,
        NotSearchableReasonsRecord(0, 0, 0, 0),
        None,
    )

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
        "repositories_not_searchable": 54,
        "not_searchable_reasons": {
            "missing_readme": 4,
            "retrieval_error": 0,
            "awaiting_indexing": 49,
            "other": 1,
        },
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
