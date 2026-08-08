from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, LiteralString, Protocol
from uuid import UUID

import psycopg
from psycopg.rows import dict_row

from app.database.pool import ConnectionProvider
from app.schemas.ingestion import IngestionStatus
from app.services.github import RepositoryWithReadme


class IngestionRepositoryError(RuntimeError):
    """A safe database-boundary failure."""


@dataclass(frozen=True, slots=True)
class IngestionRunRecord:
    run_id: UUID
    search_query: str
    started_at: datetime
    completed_at: datetime | None
    repositories_found: int
    repositories_inserted: int
    repositories_updated: int
    status: IngestionStatus
    error_message: str | None


class IngestionRepositoryProtocol(Protocol):
    async def create_run(
        self, run_id: UUID, search_query: str, started_at: datetime
    ) -> IngestionRunRecord: ...

    async def persist_completed_run(
        self,
        run_id: UUID,
        repositories: Sequence[RepositoryWithReadme],
        completed_at: datetime,
    ) -> IngestionRunRecord: ...

    async def mark_run_failed(
        self, run_id: UUID, completed_at: datetime, error_message: str
    ) -> IngestionRunRecord | None: ...

    async def get_run(self, run_id: UUID) -> IngestionRunRecord | None: ...


class IngestionRepository:
    _REPOSITORY_UPSERT: LiteralString = """
        INSERT INTO repositories (
            repo_id, name, full_name, owner, description, html_url,
            primary_language, stars, forks, open_issues, topics, license,
            created_at, updated_at, pushed_at, ingested_at
        ) VALUES (
            %s, %s, %s, %s, %s, %s,
            %s, %s, %s, %s, %s, %s,
            %s, %s, %s, %s
        )
        ON CONFLICT (repo_id) DO UPDATE SET
            name = EXCLUDED.name,
            full_name = EXCLUDED.full_name,
            owner = EXCLUDED.owner,
            description = EXCLUDED.description,
            html_url = EXCLUDED.html_url,
            primary_language = EXCLUDED.primary_language,
            stars = EXCLUDED.stars,
            forks = EXCLUDED.forks,
            open_issues = EXCLUDED.open_issues,
            topics = EXCLUDED.topics,
            license = EXCLUDED.license,
            created_at = EXCLUDED.created_at,
            updated_at = EXCLUDED.updated_at,
            pushed_at = EXCLUDED.pushed_at,
            ingested_at = EXCLUDED.ingested_at
    """

    _README_UPSERT: LiteralString = """
        INSERT INTO repository_readmes (
            repo_id, raw_content, content_hash, retrieved_at, retrieval_status
        ) VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT (repo_id) DO UPDATE SET
            raw_content = CASE
                WHEN EXCLUDED.retrieval_status = 'error'
                    THEN repository_readmes.raw_content
                ELSE EXCLUDED.raw_content
            END,
            content_hash = CASE
                WHEN EXCLUDED.retrieval_status = 'error'
                    THEN repository_readmes.content_hash
                ELSE EXCLUDED.content_hash
            END,
            retrieved_at = EXCLUDED.retrieved_at,
            retrieval_status = EXCLUDED.retrieval_status
    """

    def __init__(self, database: ConnectionProvider) -> None:
        self._database = database

    async def create_run(
        self, run_id: UUID, search_query: str, started_at: datetime
    ) -> IngestionRunRecord:
        try:
            async with self._database.connection() as connection:
                await connection.execute(
                    """
                    INSERT INTO ingestion_runs (
                        run_id, search_query, started_at, repositories_found,
                        repositories_inserted, repositories_updated, status
                    ) VALUES (%s, %s, %s, 0, 0, 0, 'running')
                    """,
                    (run_id, search_query, started_at),
                )
        except psycopg.Error as exc:
            raise IngestionRepositoryError("Unable to create the ingestion run") from exc

        return IngestionRunRecord(
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

    async def persist_completed_run(
        self,
        run_id: UUID,
        repositories: Sequence[RepositoryWithReadme],
        completed_at: datetime,
    ) -> IngestionRunRecord:
        repo_ids = [item.repository.repo_id for item in repositories]
        try:
            async with self._database.connection() as connection:
                async with connection.transaction():
                    existing_ids: set[int] = set()
                    if repo_ids:
                        cursor = await connection.execute(
                            "SELECT repo_id FROM repositories WHERE repo_id = ANY(%s)",
                            (repo_ids,),
                        )
                        existing_ids = {row[0] async for row in cursor}

                    async with connection.cursor() as cursor:
                        await cursor.executemany(
                            self._REPOSITORY_UPSERT,
                            [self._repository_parameters(item) for item in repositories],
                        )
                        await cursor.executemany(
                            self._README_UPSERT,
                            [self._readme_parameters(item) for item in repositories],
                        )

                    repositories_found = len(repositories)
                    repositories_updated = len(existing_ids)
                    repositories_inserted = repositories_found - repositories_updated
                    cursor = await connection.execute(
                        """
                        UPDATE ingestion_runs
                        SET completed_at = %s,
                            repositories_found = %s,
                            repositories_inserted = %s,
                            repositories_updated = %s,
                            status = 'completed',
                            error_message = NULL
                        WHERE run_id = %s
                        RETURNING run_id, search_query, started_at, completed_at,
                                  repositories_found, repositories_inserted,
                                  repositories_updated, status, error_message
                        """,
                        (
                            completed_at,
                            repositories_found,
                            repositories_inserted,
                            repositories_updated,
                            run_id,
                        ),
                    )
                    row = await cursor.fetchone()
                    if row is None:
                        raise IngestionRepositoryError("Ingestion run disappeared during update")
        except IngestionRepositoryError:
            raise
        except psycopg.Error as exc:
            raise IngestionRepositoryError("Unable to persist the ingestion run") from exc

        return self._record_from_row(row)

    async def mark_run_failed(
        self, run_id: UUID, completed_at: datetime, error_message: str
    ) -> IngestionRunRecord | None:
        safe_message = error_message[:2000]
        try:
            async with self._database.connection() as connection:
                async with connection.cursor(row_factory=dict_row) as cursor:
                    await cursor.execute(
                        """
                        UPDATE ingestion_runs
                        SET completed_at = %s, status = 'failed', error_message = %s
                        WHERE run_id = %s
                        RETURNING run_id, search_query, started_at, completed_at,
                                  repositories_found, repositories_inserted,
                                  repositories_updated, status, error_message
                        """,
                        (completed_at, safe_message, run_id),
                    )
                    row = await cursor.fetchone()
        except psycopg.Error as exc:
            raise IngestionRepositoryError("Unable to mark the ingestion run failed") from exc

        return self._record_from_mapping(row) if row else None

    async def get_run(self, run_id: UUID) -> IngestionRunRecord | None:
        try:
            async with self._database.connection() as connection:
                async with connection.cursor(row_factory=dict_row) as cursor:
                    await cursor.execute(
                        """
                        SELECT run_id, search_query, started_at, completed_at,
                               repositories_found, repositories_inserted,
                               repositories_updated, status, error_message
                        FROM ingestion_runs
                        WHERE run_id = %s
                        """,
                        (run_id,),
                    )
                    row = await cursor.fetchone()
        except psycopg.Error as exc:
            raise IngestionRepositoryError("Unable to retrieve the ingestion run") from exc

        return self._record_from_mapping(row) if row else None

    @staticmethod
    def _repository_parameters(item: RepositoryWithReadme) -> tuple[object, ...]:
        repository = item.repository
        return (
            repository.repo_id,
            repository.name,
            repository.full_name,
            repository.owner,
            repository.description,
            repository.html_url,
            repository.primary_language,
            repository.stars,
            repository.forks,
            repository.open_issues,
            repository.topics,
            repository.license,
            repository.created_at,
            repository.updated_at,
            repository.pushed_at,
            repository.ingested_at,
        )

    @staticmethod
    def _readme_parameters(item: RepositoryWithReadme) -> tuple[object, ...]:
        readme = item.readme
        return (
            readme.repo_id,
            readme.raw_content,
            readme.content_hash,
            readme.retrieved_at,
            readme.retrieval_status.value,
        )

    @staticmethod
    def _record_from_row(row: Sequence[Any]) -> IngestionRunRecord:
        return IngestionRunRecord(
            run_id=row[0],
            search_query=row[1],
            started_at=row[2],
            completed_at=row[3],
            repositories_found=row[4],
            repositories_inserted=row[5],
            repositories_updated=row[6],
            status=IngestionStatus(row[7]),
            error_message=row[8],
        )

    @staticmethod
    def _record_from_mapping(row: dict[str, Any]) -> IngestionRunRecord:
        return IngestionRunRecord(
            run_id=row["run_id"],
            search_query=row["search_query"],
            started_at=row["started_at"],
            completed_at=row["completed_at"],
            repositories_found=row["repositories_found"],
            repositories_inserted=row["repositories_inserted"],
            repositories_updated=row["repositories_updated"],
            status=IngestionStatus(row["status"]),
            error_message=row["error_message"],
        )
