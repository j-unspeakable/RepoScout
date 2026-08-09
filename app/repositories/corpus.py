from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

import psycopg
from psycopg.rows import dict_row

from app.database.pool import ConnectionProvider


class CorpusRepositoryError(RuntimeError):
    """A safe database-boundary failure for corpus readiness data."""


@dataclass(frozen=True, slots=True)
class CorpusSummaryRecord:
    repositories_ingested: int
    readmes_available: int
    repositories_searchable: int
    searchable_chunks: int
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
                        SELECT
                            (SELECT count(*) FROM repositories)
                                AS repositories_ingested,
                            (SELECT count(*)
                             FROM repository_readmes
                             WHERE retrieval_status = 'available')
                                AS readmes_available,
                            (SELECT count(DISTINCT repo_id) FROM repository_chunks)
                                AS repositories_searchable,
                            (SELECT count(*) FROM repository_chunks)
                                AS searchable_chunks,
                            (SELECT max(processed_at) FROM repository_chunks)
                                AS last_indexed_at
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
            last_indexed_at=row["last_indexed_at"],
        )
