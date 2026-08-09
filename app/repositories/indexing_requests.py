from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol
from uuid import UUID

import psycopg
from psycopg.rows import dict_row

from app.database.pool import ConnectionProvider
from app.schemas.indexing_requests import IndexingRequestStatus


class IndexingRequestRepositoryError(RuntimeError):
    """A safe database-boundary failure for corpus feedback."""


@dataclass(frozen=True, slots=True)
class IndexingRequestRecord:
    request_id: UUID
    search_query: str
    notes: str | None
    status: IndexingRequestStatus
    created_at: datetime


class IndexingRequestRepositoryProtocol(Protocol):
    async def create_request(
        self,
        request_id: UUID,
        search_query: str,
        notes: str | None,
        status: IndexingRequestStatus,
        created_at: datetime,
    ) -> IndexingRequestRecord: ...


class IndexingRequestRepository:
    def __init__(self, database: ConnectionProvider) -> None:
        self._database = database

    async def create_request(
        self,
        request_id: UUID,
        search_query: str,
        notes: str | None,
        status: IndexingRequestStatus,
        created_at: datetime,
    ) -> IndexingRequestRecord:
        try:
            async with self._database.connection() as connection:
                async with connection.cursor(row_factory=dict_row) as cursor:
                    await cursor.execute(
                        """
                        INSERT INTO indexing_requests (
                            request_id, search_query, notes, status, created_at
                        ) VALUES (%s, %s, %s, %s, %s)
                        RETURNING request_id, search_query, notes, status, created_at
                        """,
                        (request_id, search_query, notes, status.value, created_at),
                    )
                    row = await cursor.fetchone()
        except psycopg.Error as exc:
            raise IndexingRequestRepositoryError("Unable to persist the indexing request") from exc

        if row is None:
            raise IndexingRequestRepositoryError("Indexing request persistence returned no result")
        return self._record_from_row(row)

    @staticmethod
    def _record_from_row(row: dict[str, Any]) -> IndexingRequestRecord:
        return IndexingRequestRecord(
            request_id=row["request_id"],
            search_query=row["search_query"],
            notes=row["notes"],
            status=IndexingRequestStatus(row["status"]),
            created_at=row["created_at"],
        )
