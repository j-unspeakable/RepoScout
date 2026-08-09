from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import cast
from uuid import UUID, uuid4

import httpx
import psycopg
import pytest

from app.config import AppEnvironment, Settings
from app.database.pool import ConnectionProvider
from app.dependencies import (
    get_project_tools_service,
    get_project_user_key,
    get_retrieval_service,
)
from app.main import create_app
from app.repositories.project_tools import (
    ProjectDetailsRecord,
    ProjectEvidenceRecord,
    ProjectNoteRecord,
    ProjectToolsRepository,
    ProjectToolsRepositoryError,
    SavedProjectListRecord,
    SavedProjectRecord,
)
from app.schemas.tools import ProjectNoteCreate, ProjectStatus
from app.services.project_tools import (
    ProjectNotFoundError,
    ProjectToolsService,
    ProjectToolsUnavailableError,
    SavedProjectNotFoundError,
)
from app.services.retrieval import (
    EvidenceChunk,
    ProjectSearchResult,
    SemanticSearchResult,
)


def _saved(status: ProjectStatus = ProjectStatus.INTERESTED) -> SavedProjectRecord:
    now = datetime.now(UTC)
    return SavedProjectRecord(uuid4(), 42, status, now, now)


def _details(saved: SavedProjectRecord | None = None) -> ProjectDetailsRecord:
    return ProjectDetailsRecord(
        repo_id=42,
        name="pipeline",
        full_name="owner/pipeline",
        owner="owner",
        description="Pipelines",
        html_url="https://github.com/owner/pipeline",
        primary_language="Python",
        stars=100,
        forks=5,
        open_issues=2,
        topics=["data-engineering"],
        license="MIT",
        evidence=[ProjectEvidenceRecord("chunk-0", 0, "README evidence")],
        saved_project=saved,
        notes=[],
    )


def _listed_project(notes: list[ProjectNoteRecord] | None = None) -> SavedProjectListRecord:
    now = datetime.now(UTC)
    return SavedProjectListRecord(
        repo_id=42,
        name="pipeline",
        full_name="owner/pipeline",
        owner="owner",
        description="Pipelines",
        html_url="https://github.com/owner/pipeline",
        primary_language="Python",
        stars=100,
        forks=5,
        open_issues=2,
        topics=["data-engineering"],
        license="MIT",
        status=ProjectStatus.INTERESTED,
        saved_at=now,
        updated_at=now,
        notes=notes or [],
    )


class FakeProjectRepository:
    def __init__(self) -> None:
        self.saved: SavedProjectRecord | None = None
        self.notes: list[ProjectNoteRecord] = []
        self.fail = False

    async def list_saved_projects(self, user_key: str) -> list[SavedProjectListRecord]:
        if self.fail:
            raise ProjectToolsRepositoryError("database secret")
        return [_listed_project(self.notes)] if self.saved is not None else []

    async def get_project_details(
        self, user_key: str, repo_id: int, evidence_limit: int
    ) -> ProjectDetailsRecord | None:
        if self.fail:
            raise ProjectToolsRepositoryError("database secret")
        return _details(self.saved) if repo_id == 42 else None

    async def save_project(
        self,
        user_key: str,
        repo_id: int,
        saved_project_id: UUID,
        saved_at: datetime,
    ) -> SavedProjectRecord | None:
        if self.fail:
            raise ProjectToolsRepositoryError("database secret")
        if repo_id != 42:
            return None
        if self.saved is None:
            self.saved = SavedProjectRecord(
                saved_project_id, repo_id, ProjectStatus.INTERESTED, saved_at, saved_at
            )
        return self.saved

    async def update_project_status(
        self,
        user_key: str,
        repo_id: int,
        project_status: ProjectStatus,
        updated_at: datetime,
    ) -> SavedProjectRecord | None:
        if self.saved is None:
            return None
        self.saved = SavedProjectRecord(
            self.saved.saved_project_id,
            repo_id,
            project_status,
            self.saved.saved_at,
            updated_at,
        )
        return self.saved

    async def add_project_note(
        self,
        user_key: str,
        repo_id: int,
        note_id: UUID,
        note: str,
        created_at: datetime,
    ) -> ProjectNoteRecord | None:
        if self.saved is None:
            return None
        record = ProjectNoteRecord(note_id, note, created_at)
        self.notes.append(record)
        return record


class FakeCursor:
    def __init__(
        self,
        rows: list[dict[str, object] | None],
        batches: list[list[dict[str, object]]] | None = None,
    ) -> None:
        self.rows = rows
        self.batches = batches or []
        self.statements: list[str] = []

    async def __aenter__(self) -> "FakeCursor":
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    async def execute(self, query: str, parameters: object) -> None:
        self.statements.append(query)

    async def fetchone(self) -> dict[str, object] | None:
        return self.rows.pop(0)

    async def fetchall(self) -> list[dict[str, object]]:
        return self.batches.pop(0)


class FakeConnection:
    def __init__(self, cursor: FakeCursor) -> None:
        self._cursor = cursor

    def cursor(self, **kwargs: object) -> FakeCursor:
        return self._cursor

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[None]:
        yield


class FakeDatabase:
    def __init__(self, connection: FakeConnection | None = None) -> None:
        self._connection = connection

    @asynccontextmanager
    async def connection(self) -> AsyncIterator[FakeConnection]:
        if self._connection is None:
            raise psycopg.OperationalError("database secret")
        yield self._connection


class FakeRetrieval:
    async def search(
        self,
        query: str,
        top_k: int,
        language: str | None = None,
        minimum_stars: int | None = None,
    ) -> SemanticSearchResult:
        project = ProjectSearchResult(
            rank=1,
            repo_id=42,
            name="pipeline",
            full_name="owner/pipeline",
            owner="owner",
            description="Pipelines",
            html_url="https://github.com/owner/pipeline",
            primary_language="Python",
            stars=100,
            forks=5,
            open_issues=2,
            topics=["data-engineering"],
            license="MIT",
            similarity=0.7,
            evidence=[EvidenceChunk("chunk-0", 0, "evidence", 0.7)],
        )
        return SemanticSearchResult(query, "model", [project])


class FakeProjectService:
    def __init__(self) -> None:
        self.saved = _saved()
        self.notes: list[ProjectNoteRecord] = []

    async def list_saved_projects(self, user_key: str) -> list[SavedProjectListRecord]:
        return [_listed_project(self.notes)]

    async def get_project_details(
        self, user_key: str, repo_id: int, evidence_limit: int
    ) -> ProjectDetailsRecord:
        if repo_id != 42:
            raise ProjectNotFoundError("Repository not found")
        details = _details(self.saved)
        return ProjectDetailsRecord(
            **{
                field: getattr(details, field)
                for field in (
                    "repo_id",
                    "name",
                    "full_name",
                    "owner",
                    "description",
                    "html_url",
                    "primary_language",
                    "stars",
                    "forks",
                    "open_issues",
                    "topics",
                    "license",
                    "evidence",
                    "saved_project",
                )
            },
            notes=self.notes[:10],
        )

    async def save_project(self, user_key: str, repo_id: int) -> SavedProjectRecord:
        if repo_id != 42:
            raise ProjectNotFoundError("Repository not found")
        return self.saved

    async def update_project_status(
        self, user_key: str, repo_id: int, project_status: ProjectStatus
    ) -> SavedProjectRecord:
        if repo_id != 42:
            raise SavedProjectNotFoundError("Saved project not found")
        self.saved = SavedProjectRecord(
            self.saved.saved_project_id,
            repo_id,
            project_status,
            self.saved.saved_at,
            datetime.now(UTC),
        )
        return self.saved

    async def add_project_note(self, user_key: str, repo_id: int, note: str) -> ProjectNoteRecord:
        if repo_id != 42:
            raise SavedProjectNotFoundError("Saved project not found")
        record = ProjectNoteRecord(uuid4(), note, datetime.now(UTC))
        self.notes.insert(0, record)
        return record


def test_project_state_schema_and_repository_contract_are_bounded_and_idempotent() -> None:
    assert ProjectNoteCreate(note="  useful  ").note == "useful"
    source = (Path(__file__).parents[1] / "app" / "repositories" / "project_tools.py").read_text()
    assert "ON CONFLICT (user_key, repo_id) DO NOTHING" in source
    assert "ON CONFLICT (user_key, repo_id) DO UPDATE" not in source
    assert "ORDER BY chunk_index ASC, chunk_id ASC" in source
    assert "ORDER BY created_at DESC, note_id ASC" in source
    assert "LIMIT 10" in source


@pytest.mark.asyncio
async def test_repository_idempotent_insert_reads_the_existing_record() -> None:
    existing = _saved(ProjectStatus.COMPLETED)
    cursor = FakeCursor(
        [
            {"repo_id": 42},
            {
                "saved_project_id": existing.saved_project_id,
                "repo_id": existing.repo_id,
                "status": existing.status.value,
                "saved_at": existing.saved_at,
                "updated_at": existing.updated_at,
            },
        ]
    )
    database = FakeDatabase(FakeConnection(cursor))
    repository = ProjectToolsRepository(cast(ConnectionProvider, database))

    result = await repository.save_project("default", 42, uuid4(), datetime.now(UTC))

    assert result == existing
    assert "ON CONFLICT (user_key, repo_id) DO NOTHING" in cursor.statements[1]
    assert "SELECT saved_project_id" in cursor.statements[2]

    unavailable = ProjectToolsRepository(cast(ConnectionProvider, FakeDatabase()))
    with pytest.raises(ProjectToolsRepositoryError, match="Unable to save project") as caught:
        await unavailable.save_project("default", 42, uuid4(), datetime.now(UTC))
    assert "database secret" not in str(caught.value)


@pytest.mark.asyncio
async def test_service_repeated_save_preserves_existing_record_and_requires_save_for_writes() -> (
    None
):
    repository = FakeProjectRepository()
    service = ProjectToolsService(repository)

    first = await service.save_project("default", 42)
    updated = await service.update_project_status("default", 42, ProjectStatus.IN_PROGRESS)
    second = await service.save_project("default", 42)

    assert second == updated
    assert second.saved_project_id == first.saved_project_id
    assert second.saved_at == first.saved_at
    assert second.status is ProjectStatus.IN_PROGRESS
    with pytest.raises(SavedProjectNotFoundError):
        await ProjectToolsService(FakeProjectRepository()).add_project_note("default", 42, "note")


@pytest.mark.asyncio
async def test_saved_project_listing_uses_two_bounded_queries_and_safe_failures() -> None:
    now = datetime.now(UTC)
    saved_project_id = uuid4()
    note_id = uuid4()
    project_row: dict[str, object] = {
        "saved_project_id": saved_project_id,
        "status": "IN_PROGRESS",
        "saved_at": now,
        "updated_at": now,
        "repo_id": 42,
        "name": "pipeline",
        "full_name": "owner/pipeline",
        "owner": "owner",
        "description": "Pipelines",
        "html_url": "https://github.com/owner/pipeline",
        "primary_language": "Python",
        "stars": 100,
        "forks": 5,
        "open_issues": 2,
        "topics": ["data-engineering"],
        "license": "MIT",
    }
    note_row: dict[str, object] = {
        "saved_project_id": saved_project_id,
        "note_id": note_id,
        "note_text": "Evaluate setup",
        "created_at": now,
    }
    cursor = FakeCursor([], [[project_row], [note_row]])
    repository = ProjectToolsRepository(
        cast(ConnectionProvider, FakeDatabase(FakeConnection(cursor)))
    )

    records = await repository.list_saved_projects("default")

    assert records[0].repo_id == 42
    assert records[0].notes[0].note == "Evaluate setup"
    assert len(cursor.statements) == 2
    assert "ORDER BY sp.updated_at DESC, r.repo_id ASC" in cursor.statements[0]
    assert "LIMIT 10" in cursor.statements[1]
    assert "recent.created_at DESC, recent.note_id ASC" in cursor.statements[1]

    unavailable = ProjectToolsService(
        ProjectToolsRepository(cast(ConnectionProvider, FakeDatabase()))
    )
    with pytest.raises(ProjectToolsUnavailableError, match="Saved projects unavailable"):
        await unavailable.list_saved_projects("default")


@asynccontextmanager
async def _client() -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(Settings(app_env=AppEnvironment.TEST))
    project_service = FakeProjectService()

    async def project_override() -> FakeProjectService:
        return project_service

    async def retrieval_override() -> FakeRetrieval:
        return FakeRetrieval()

    async def user_override() -> str:
        return "default"

    app.dependency_overrides[get_project_tools_service] = project_override
    app.dependency_overrides[get_retrieval_service] = retrieval_override
    app.dependency_overrides[get_project_user_key] = user_override
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            yield client


@pytest.mark.asyncio
async def test_all_machine_tool_endpoints_and_validation() -> None:
    async with _client() as client:
        search = await client.post("/api/tools/search-projects", json={"query": " pipelines "})
        details = await client.get("/api/tools/projects/42?evidence_limit=1")
        saved = await client.put("/api/tools/saved-projects/42")
        updated = await client.patch(
            "/api/tools/saved-projects/42/status", json={"status": "COMPLETED"}
        )
        note = await client.post(
            "/api/tools/saved-projects/42/notes", json={"note": "  Try this  "}
        )
        final_details = await client.get("/api/tools/projects/42")
        invalid = await client.patch(
            "/api/tools/saved-projects/42/status", json={"status": "UNKNOWN"}
        )
        missing = await client.put("/api/tools/saved-projects/999")

    assert search.status_code == 200
    assert search.json()["projects"][0]["repo_id"] == 42
    assert details.status_code == 200
    assert len(details.json()["evidence"]) == 1
    assert saved.status_code == 200
    assert updated.json()["status"] == "COMPLETED"
    assert note.status_code == 201
    assert note.json()["note"] == "Try this"
    assert final_details.json()["notes"][0]["note"] == "Try this"
    assert invalid.status_code == 422
    assert missing.status_code == 404


@pytest.mark.asyncio
async def test_browser_saved_projects_endpoint_is_typed_and_read_only() -> None:
    async with _client() as client:
        response = await client.get("/saved-projects")

    assert response.status_code == 200
    assert response.json()["projects"][0]["full_name"] == "owner/pipeline"
    assert response.json()["projects"][0]["status"] == "INTERESTED"
    assert "saved_project_id" not in response.text


@pytest.mark.asyncio
async def test_machine_tools_missing_dependency_is_safe() -> None:
    app = create_app(Settings(app_env=AppEnvironment.TEST))
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            response = await client.get("/api/tools/projects/42")

    assert response.status_code == 503
    assert response.json() == {"detail": "Project tool dependencies are unavailable"}


def test_default_user_boundary_is_internal() -> None:
    import asyncio

    assert asyncio.run(get_project_user_key()) == "default"
    assert "user_key" not in ProjectNoteCreate.model_fields
