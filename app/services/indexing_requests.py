from datetime import UTC, datetime
from uuid import uuid4

from app.repositories.indexing_requests import (
    IndexingRequestRecord,
    IndexingRequestRepositoryError,
    IndexingRequestRepositoryProtocol,
)
from app.schemas.indexing_requests import IndexingRequestStatus


class IndexingRequestUnavailableError(RuntimeError):
    pass


class IndexingRequestService:
    def __init__(self, repository: IndexingRequestRepositoryProtocol) -> None:
        self._repository = repository

    async def create_request(
        self,
        search_query: str,
        notes: str | None,
    ) -> IndexingRequestRecord:
        try:
            return await self._repository.create_request(
                request_id=uuid4(),
                search_query=search_query,
                notes=notes,
                status=IndexingRequestStatus.NEW,
                created_at=datetime.now(UTC),
            )
        except IndexingRequestRepositoryError as exc:
            raise IndexingRequestUnavailableError("Unable to submit indexing request") from exc
