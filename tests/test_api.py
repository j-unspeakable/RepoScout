from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from uuid import UUID, uuid4

import httpx
import pytest

from app.config import AppEnvironment, Settings
from app.dependencies import get_ingestion_service
from app.main import create_app
from app.repositories.ingestion import IngestionRunRecord
from app.schemas.ingestion import IngestionStatus
from app.services.ingestion import (
    IngestionDatabaseFailure,
    IngestionRateLimitFailure,
    IngestionSearchFailure,
)


def _run_record(run_id: UUID | None = None) -> IngestionRunRecord:
    now = datetime.now(UTC)
    return IngestionRunRecord(
        run_id=run_id or uuid4(),
        search_query="fastapi",
        started_at=now,
        completed_at=now,
        repositories_found=2,
        repositories_inserted=2,
        repositories_updated=0,
        status=IngestionStatus.COMPLETED,
        error_message=None,
    )


class FakeIngestionService:
    def __init__(self, record: IngestionRunRecord | None = None) -> None:
        self.record = record or _run_record()
        self.ingest_calls: list[tuple[str, int]] = []
        self.error: Exception | None = None

    async def ingest(self, search_query: str, max_repositories: int) -> IngestionRunRecord:
        self.ingest_calls.append((search_query, max_repositories))
        if self.error:
            raise self.error
        return self.record

    async def get_run(self, run_id: UUID) -> IngestionRunRecord | None:
        if self.error:
            raise self.error
        return self.record if run_id == self.record.run_id else None


@asynccontextmanager
async def _client(
    service: FakeIngestionService | None = None,
) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(Settings(app_env=AppEnvironment.TEST))
    if service:

        async def override_ingestion_service() -> FakeIngestionService:
            return service

        app.dependency_overrides[get_ingestion_service] = override_ingestion_service
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            yield client


@pytest.mark.asyncio
async def test_health_endpoint() -> None:
    async with _client() as client:
        response = await client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "environment": "test"}


@pytest.mark.asyncio
async def test_ingestion_defaults_to_thirty_repositories() -> None:
    service = FakeIngestionService()
    async with _client(service) as client:
        response = await client.post("/ingestions", json={"search_query": "  fastapi  "})

    assert response.status_code == 201
    assert service.ingest_calls == [("fastapi", 30)]
    assert response.json()["status"] == "completed"


@pytest.mark.asyncio
async def test_ingestion_accepts_hard_maximum_and_rejects_more() -> None:
    service = FakeIngestionService()
    async with _client(service) as client:
        accepted = await client.post(
            "/ingestions", json={"search_query": "rag", "max_repositories": 100}
        )
        rejected = await client.post(
            "/ingestions", json={"search_query": "rag", "max_repositories": 101}
        )

    assert accepted.status_code == 201
    assert rejected.status_code == 422


@pytest.mark.asyncio
async def test_get_ingestion_returns_record_or_not_found() -> None:
    service = FakeIngestionService()
    async with _client(service) as client:
        found = await client.get(f"/ingestions/{service.record.run_id}")
        missing = await client.get(f"/ingestions/{uuid4()}")

    assert found.status_code == 200
    assert missing.status_code == 404


@pytest.mark.asyncio
async def test_ingestion_error_mapping() -> None:
    run_id = uuid4()
    cases = (
        (IngestionSearchFailure("search failed", run_id), 502),
        (IngestionDatabaseFailure("database unavailable", run_id), 503),
        (IngestionRateLimitFailure("rate limited", run_id, 17), 503),
    )
    for error, expected_status in cases:
        service = FakeIngestionService()
        service.error = error
        async with _client(service) as client:
            response = await client.post("/ingestions", json={"search_query": "fastapi"})
        assert response.status_code == expected_status
        assert response.json()["detail"]["run_id"] == str(run_id)
        if isinstance(error, IngestionRateLimitFailure):
            assert response.headers["Retry-After"] == "17"


@pytest.mark.asyncio
async def test_ingestion_dependency_is_unavailable_without_test_override() -> None:
    async with _client() as client:
        response = await client.post("/ingestions", json={"search_query": "fastapi"})

    assert response.status_code == 503
