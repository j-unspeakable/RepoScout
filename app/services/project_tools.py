from datetime import UTC, datetime
from uuid import uuid4

from app.repositories.project_tools import (
    ProjectDetailsRecord,
    ProjectNoteRecord,
    ProjectToolsRepositoryError,
    ProjectToolsRepositoryProtocol,
    SavedProjectRecord,
)
from app.schemas.tools import ProjectStatus


class ProjectToolsUnavailableError(RuntimeError):
    pass


class ProjectNotFoundError(RuntimeError):
    pass


class SavedProjectNotFoundError(RuntimeError):
    pass


class ProjectToolsService:
    def __init__(self, repository: ProjectToolsRepositoryProtocol) -> None:
        self._repository = repository

    async def get_project_details(
        self, user_key: str, repo_id: int, evidence_limit: int
    ) -> ProjectDetailsRecord:
        try:
            record = await self._repository.get_project_details(user_key, repo_id, evidence_limit)
        except ProjectToolsRepositoryError as exc:
            raise ProjectToolsUnavailableError("Project details unavailable") from exc
        if record is None:
            raise ProjectNotFoundError("Repository not found")
        return record

    async def save_project(self, user_key: str, repo_id: int) -> SavedProjectRecord:
        try:
            record = await self._repository.save_project(
                user_key,
                repo_id,
                uuid4(),
                datetime.now(UTC),
            )
        except ProjectToolsRepositoryError as exc:
            raise ProjectToolsUnavailableError("Unable to save project") from exc
        if record is None:
            raise ProjectNotFoundError("Repository not found")
        return record

    async def update_project_status(
        self, user_key: str, repo_id: int, project_status: ProjectStatus
    ) -> SavedProjectRecord:
        try:
            record = await self._repository.update_project_status(
                user_key,
                repo_id,
                project_status,
                datetime.now(UTC),
            )
        except ProjectToolsRepositoryError as exc:
            raise ProjectToolsUnavailableError("Unable to update project status") from exc
        if record is None:
            raise SavedProjectNotFoundError("Saved project not found")
        return record

    async def add_project_note(self, user_key: str, repo_id: int, note: str) -> ProjectNoteRecord:
        try:
            record = await self._repository.add_project_note(
                user_key,
                repo_id,
                uuid4(),
                note,
                datetime.now(UTC),
            )
        except ProjectToolsRepositoryError as exc:
            raise ProjectToolsUnavailableError("Unable to add project note") from exc
        if record is None:
            raise SavedProjectNotFoundError("Saved project not found")
        return record
