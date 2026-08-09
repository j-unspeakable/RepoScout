from app.repositories.corpus import (
    CorpusRepositoryError,
    CorpusRepositoryProtocol,
    CorpusSummaryRecord,
)


class CorpusUnavailableError(RuntimeError):
    pass


class CorpusService:
    def __init__(self, repository: CorpusRepositoryProtocol) -> None:
        self._repository = repository

    async def get_summary(self) -> CorpusSummaryRecord:
        try:
            return await self._repository.get_summary()
        except CorpusRepositoryError as exc:
            raise CorpusUnavailableError("Corpus summary unavailable") from exc
