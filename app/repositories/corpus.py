from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

import psycopg
from psycopg.rows import dict_row

from app.database.pool import ConnectionProvider


class CorpusRepositoryError(RuntimeError):
    """A safe database-boundary failure for corpus readiness data."""


@dataclass(frozen=True, slots=True)
class NotSearchableReasonsRecord:
    missing_readme: int
    retrieval_error: int
    awaiting_indexing: int
    other: int


@dataclass(frozen=True, slots=True)
class CorpusSummaryRecord:
    repositories_ingested: int
    readmes_available: int
    repositories_searchable: int
    searchable_chunks: int
    repositories_not_searchable: int
    not_searchable_reasons: NotSearchableReasonsRecord
    last_indexed_at: datetime | None


class CorpusRepositoryProtocol(Protocol):
    async def get_summary(self) -> CorpusSummaryRecord: ...


class CorpusRepository:
    def __init__(self, database: ConnectionProvider) -> None:
        self._database = database

    async def get_summary(self) -> CorpusSummaryRecord:
        try:
            async with self._database.connection() as connection:
                async with connection.cursor(row_factory=dict_row) as cursor:
                    await cursor.execute(
                        """
                        WITH chunk_state AS (
                            SELECT repo_id,
                                   count(*) AS chunk_count,
                                   max(processed_at) AS last_indexed_at
                            FROM repository_chunks
                            GROUP BY repo_id
                        ),
                        repository_state AS (
                            SELECT r.repo_id,
                                   rr.retrieval_status,
                                   COALESCE(cs.chunk_count, 0) > 0 AS has_chunks,
                                   COALESCE(cs.chunk_count, 0) AS chunk_count,
                                   cs.last_indexed_at
                            FROM repositories AS r
                            LEFT JOIN repository_readmes AS rr ON rr.repo_id = r.repo_id
                            LEFT JOIN chunk_state AS cs ON cs.repo_id = r.repo_id
                        )
                        SELECT
                            count(*) AS repositories_ingested,
                            count(*) FILTER (WHERE retrieval_status = 'available')
                                AS readmes_available,
                            count(*) FILTER (WHERE has_chunks)
                                AS repositories_searchable,
                            COALESCE(sum(chunk_count), 0) AS searchable_chunks,
                            count(*) FILTER (WHERE NOT has_chunks)
                                AS repositories_not_searchable,
                            count(*) FILTER (
                                WHERE NOT has_chunks AND retrieval_status = 'missing'
                            ) AS missing_readme,
                            count(*) FILTER (
                                WHERE NOT has_chunks AND retrieval_status = 'error'
                            ) AS retrieval_error,
                            count(*) FILTER (
                                WHERE NOT has_chunks AND retrieval_status = 'available'
                            ) AS awaiting_indexing,
                            count(*) FILTER (
                                WHERE NOT has_chunks
                                  AND (
                                      retrieval_status IS NULL
                                      OR retrieval_status NOT IN ('missing', 'error', 'available')
                                  )
                            ) AS other,
                            max(last_indexed_at) AS last_indexed_at
                        FROM repository_state
                        """
                    )
                    row = await cursor.fetchone()
        except psycopg.Error as exc:
            raise CorpusRepositoryError("Unable to retrieve corpus summary") from exc

        if row is None:
            raise CorpusRepositoryError("Corpus summary query returned no result")
        return self._record_from_row(row)

    @staticmethod
    def _record_from_row(row: dict[str, Any]) -> CorpusSummaryRecord:
        return CorpusSummaryRecord(
            repositories_ingested=int(row["repositories_ingested"]),
            readmes_available=int(row["readmes_available"]),
            repositories_searchable=int(row["repositories_searchable"]),
            searchable_chunks=int(row["searchable_chunks"]),
            repositories_not_searchable=int(row["repositories_not_searchable"]),
            not_searchable_reasons=NotSearchableReasonsRecord(
                missing_readme=int(row["missing_readme"]),
                retrieval_error=int(row["retrieval_error"]),
                awaiting_indexing=int(row["awaiting_indexing"]),
                other=int(row["other"]),
            ),
            last_indexed_at=row["last_indexed_at"],
        )
