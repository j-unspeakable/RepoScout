from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID, uuid4

import httpx
import psycopg
import pytest
from pydantic import ValidationError

from app.config import AppEnvironment, Settings
from app.database.pool import ConnectionProvider
from app.dependencies import get_indexing_request_service
from app.main import create_app
from app.repositories.indexing_requests import (
    IndexingRequestRecord,
    IndexingRequestRepository,
    IndexingRequestRepositoryError,
)
from app.schemas.indexing_requests import IndexingRequestCreate, IndexingRequestStatus
from app.services.indexing_requests import (
    IndexingRequestService,
    IndexingRequestUnavailableError,
)


class FakeCursor:
    def __init__(self, row: dict[str, Any] | None) -> None:
        self.row = row
        self.executed_sql = ""
        self.parameters: tuple[object, ...] | None = None

    async def __aenter__(self) -> "FakeCursor":
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    async def execute(self, query: str, parameters: tuple[object, ...]) -> None:
        self.executed_sql = query
        self.parameters = parameters

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


class FakeIndexingRequestRepository:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.calls: list[tuple[UUID, str, str | None, IndexingRequestStatus, datetime]] = []

    async def create_request(
        self,
        request_id: UUID,
        search_query: str,
        notes: str | None,
        status: IndexingRequestStatus,
        created_at: datetime,
    ) -> IndexingRequestRecord:
        self.calls.append((request_id, search_query, notes, status, created_at))
        if self.error is not None:
            raise self.error
        return IndexingRequestRecord(request_id, search_query, notes, status, created_at)


class FakeIndexingRequestService:
    def __init__(
        self,
        record: IndexingRequestRecord | None = None,
        error: Exception | None = None,
    ) -> None:
        self.record = record or _record()
        self.error = error
        self.calls: list[tuple[str, str | None]] = []

    async def create_request(self, search_query: str, notes: str | None) -> IndexingRequestRecord:
        self.calls.append((search_query, notes))
        if self.error is not None:
            raise self.error
        return self.record


def _record() -> IndexingRequestRecord:
    return IndexingRequestRecord(
        request_id=uuid4(),
        search_query="Streaming data quality tools",
        notes=None,
        status=IndexingRequestStatus.NEW,
        created_at=datetime.now(UTC),
    )


def test_indexing_request_schema_normalizes_and_validates_natural_language() -> None:
    request = IndexingRequestCreate(
        search_query="  Streaming data quality tools  ",
        notes="   ",
    )

    assert request.search_query == "Streaming data quality tools"
    assert request.notes is None
    assert "suggested_repository_url" not in IndexingRequestCreate.model_fields
    assert [status.value for status in IndexingRequestStatus] == [
        "NEW",
        "REVIEWED",
        "COVERED",
        "DECLINED",
    ]
    assert "INDEXED" not in IndexingRequestStatus.__members__

    invalid = (
        {"search_query": "   "},
        {"search_query": "x" * 501},
        {"search_query": "topic", "notes": "x" * 2001},
    )
    for payload in invalid:
        with pytest.raises(ValidationError):
            IndexingRequestCreate.model_validate(payload)


@pytest.mark.asyncio
async def test_repository_uses_parameterized_insert_and_maps_database_failure() -> None:
    record = _record()
    cursor = FakeCursor(
        {
            "request_id": record.request_id,
            "search_query": record.search_query,
            "notes": record.notes,
            "status": record.status.value,
            "created_at": record.created_at,
        }
    )
    repository = IndexingRequestRepository(cast(ConnectionProvider, FakeDatabase(cursor)))

    result = await repository.create_request(
        record.request_id,
        record.search_query,
        record.notes,
        record.status,
        record.created_at,
    )

    assert result == record
    assert "INSERT INTO indexing_requests" in cursor.executed_sql
    assert "RETURNING request_id, search_query, notes, status, created_at" in cursor.executed_sql
    assert cursor.parameters == (
        record.request_id,
        record.search_query,
        None,
        "NEW",
        record.created_at,
    )

    unavailable = IndexingRequestRepository(
        cast(
            ConnectionProvider,
            FakeDatabase(error=psycopg.OperationalError("database secret")),
        )
    )
    with pytest.raises(
        IndexingRequestRepositoryError, match="Unable to persist the indexing request"
    ) as caught:
        await unavailable.create_request(
            record.request_id,
            record.search_query,
            None,
            record.status,
            record.created_at,
        )
    assert "database secret" not in str(caught.value)


@pytest.mark.asyncio
async def test_service_accepts_duplicate_demand_and_maps_failure() -> None:
    repository = FakeIndexingRequestRepository()
    service = IndexingRequestService(repository)

    first = await service.create_request("same need", None)
    second = await service.create_request("same need", None)

    assert first.request_id != second.request_id
    assert len(repository.calls) == 2
    assert all(call[3] is IndexingRequestStatus.NEW for call in repository.calls)
    assert all(call[4].tzinfo is UTC for call in repository.calls)

    unavailable = IndexingRequestService(
        FakeIndexingRequestRepository(error=IndexingRequestRepositoryError("database secret"))
    )
    with pytest.raises(
        IndexingRequestUnavailableError, match="Unable to submit indexing request"
    ) as caught:
        await unavailable.create_request("topic", None)
    assert "database secret" not in str(caught.value)


@asynccontextmanager
async def _client(
    service: FakeIndexingRequestService | None = None,
) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(Settings(app_env=AppEnvironment.TEST))
    if service is not None:

        async def override_service() -> FakeIndexingRequestService:
            return service

        app.dependency_overrides[get_indexing_request_service] = override_service
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            yield client


@pytest.mark.asyncio
async def test_endpoint_returns_typed_created_response_and_validation_errors() -> None:
    service = FakeIndexingRequestService()
    async with _client(service) as client:
        created = await client.post(
            "/indexing-requests",
            json={"search_query": "  Streaming data quality tools  ", "notes": "   "},
        )
        blank = await client.post("/indexing-requests", json={"search_query": "   "})
        schema = await client.get("/openapi.json")

    assert created.status_code == 201
    assert service.calls == [("Streaming data quality tools", None)]
    assert created.json() == {
        "request_id": str(service.record.request_id),
        "search_query": service.record.search_query,
        "notes": None,
        "status": "NEW",
        "created_at": service.record.created_at.isoformat().replace("+00:00", "Z"),
    }
    assert blank.status_code == 422
    assert "/indexing-requests" in schema.json()["paths"]


@pytest.mark.asyncio
async def test_endpoint_maps_safe_failure_and_missing_dependency() -> None:
    failing = FakeIndexingRequestService(
        error=IndexingRequestUnavailableError("Unable to submit indexing request")
    )
    async with _client(failing) as client:
        failure = await client.post("/indexing-requests", json={"search_query": "topic"})
    async with _client() as client:
        unavailable = await client.post("/indexing-requests", json={"search_query": "topic"})

    assert failure.status_code == 503
    assert failure.json() == {"detail": "Unable to submit indexing request"}
    assert unavailable.status_code == 503
    assert unavailable.json() == {"detail": "Indexing request dependencies are unavailable"}
