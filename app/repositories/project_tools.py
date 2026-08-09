from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol
from uuid import UUID

import psycopg
from psycopg.rows import dict_row

from app.database.pool import ConnectionProvider
from app.schemas.tools import ProjectStatus


class ProjectToolsRepositoryError(RuntimeError):
    """A safe database-boundary failure for project tools."""


@dataclass(frozen=True, slots=True)
class SavedProjectRecord:
    saved_project_id: UUID
    repo_id: int
    status: ProjectStatus
    saved_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class ProjectNoteRecord:
    note_id: UUID
    note: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class ProjectEvidenceRecord:
    chunk_id: str
    chunk_index: int
    chunk_text: str


@dataclass(frozen=True, slots=True)
class ProjectDetailsRecord:
    repo_id: int
    name: str
    full_name: str
    owner: str
    description: str | None
    html_url: str
    primary_language: str | None
    stars: int
    forks: int
    open_issues: int
    topics: list[str]
    license: str | None
    evidence: list[ProjectEvidenceRecord]
    saved_project: SavedProjectRecord | None
    notes: list[ProjectNoteRecord]


@dataclass(frozen=True, slots=True)
class SavedProjectListRecord:
    repo_id: int
    name: str
    full_name: str
    owner: str
    description: str | None
    html_url: str
    primary_language: str | None
    stars: int
    forks: int
    open_issues: int
    topics: list[str]
    license: str | None
    status: ProjectStatus
    saved_at: datetime
    updated_at: datetime
    notes: list[ProjectNoteRecord]


class ProjectToolsRepositoryProtocol(Protocol):
    async def list_saved_projects(self, user_key: str) -> list[SavedProjectListRecord]: ...

    async def get_project_details(
        self, user_key: str, repo_id: int, evidence_limit: int
    ) -> ProjectDetailsRecord | None: ...

    async def save_project(
        self,
        user_key: str,
        repo_id: int,
        saved_project_id: UUID,
        saved_at: datetime,
    ) -> SavedProjectRecord | None: ...

    async def update_project_status(
        self,
        user_key: str,
        repo_id: int,
        project_status: ProjectStatus,
        updated_at: datetime,
    ) -> SavedProjectRecord | None: ...

    async def add_project_note(
        self,
        user_key: str,
        repo_id: int,
        note_id: UUID,
        note: str,
        created_at: datetime,
    ) -> ProjectNoteRecord | None: ...


class ProjectToolsRepository:
    def __init__(self, database: ConnectionProvider) -> None:
        self._database = database

    async def list_saved_projects(self, user_key: str) -> list[SavedProjectListRecord]:
        try:
            async with self._database.connection() as connection:
                async with connection.cursor(row_factory=dict_row) as cursor:
                    await cursor.execute(
                        """
                        SELECT sp.saved_project_id, sp.status, sp.saved_at, sp.updated_at,
                               r.repo_id, r.name, r.full_name, r.owner, r.description,
                               r.html_url, r.primary_language, r.stars, r.forks,
                               r.open_issues, r.topics, r.license
                        FROM saved_projects AS sp
                        JOIN repositories AS r ON r.repo_id = sp.repo_id
                        WHERE sp.user_key = %s
                        ORDER BY sp.updated_at DESC, r.repo_id ASC
                        """,
                        (user_key,),
                    )
                    project_rows = await cursor.fetchall()
                    if not project_rows:
                        return []

                    await cursor.execute(
                        """
                        SELECT sp.saved_project_id, recent.note_id,
                               recent.note_text, recent.created_at
                        FROM saved_projects AS sp
                        JOIN LATERAL (
                            SELECT pn.note_id, pn.note_text, pn.created_at
                            FROM project_notes AS pn
                            WHERE pn.saved_project_id = sp.saved_project_id
                            ORDER BY pn.created_at DESC, pn.note_id ASC
                            LIMIT 10
                        ) AS recent ON TRUE
                        WHERE sp.user_key = %s
                        ORDER BY sp.saved_project_id ASC,
                                 recent.created_at DESC, recent.note_id ASC
                        """,
                        (user_key,),
                    )
                    note_rows = await cursor.fetchall()
        except psycopg.Error as exc:
            raise ProjectToolsRepositoryError("Unable to list saved projects") from exc

        notes_by_project: dict[UUID, list[ProjectNoteRecord]] = {}
        for row in note_rows:
            notes_by_project.setdefault(row["saved_project_id"], []).append(
                self._note_from_row(row)
            )
        return [
            SavedProjectListRecord(
                repo_id=row["repo_id"],
                name=row["name"],
                full_name=row["full_name"],
                owner=row["owner"],
                description=row["description"],
                html_url=row["html_url"],
                primary_language=row["primary_language"],
                stars=row["stars"],
                forks=row["forks"],
                open_issues=row["open_issues"],
                topics=list(row["topics"]),
                license=row["license"],
                status=ProjectStatus(row["status"]),
                saved_at=row["saved_at"],
                updated_at=row["updated_at"],
                notes=notes_by_project.get(row["saved_project_id"], []),
            )
            for row in project_rows
        ]

    async def get_project_details(
        self, user_key: str, repo_id: int, evidence_limit: int
    ) -> ProjectDetailsRecord | None:
        try:
            async with self._database.connection() as connection:
                async with connection.cursor(row_factory=dict_row) as cursor:
                    await cursor.execute(
                        """
                        SELECT r.repo_id, r.name, r.full_name, r.owner, r.description,
                               r.html_url, r.primary_language, r.stars, r.forks,
                               r.open_issues, r.topics, r.license,
                               sp.saved_project_id, sp.status AS saved_status,
                               sp.saved_at, sp.updated_at
                        FROM repositories AS r
                        LEFT JOIN saved_projects AS sp
                          ON sp.repo_id = r.repo_id AND sp.user_key = %s
                        WHERE r.repo_id = %s
                        """,
                        (user_key, repo_id),
                    )
                    project = await cursor.fetchone()
                    if project is None:
                        return None

                    await cursor.execute(
                        """
                        SELECT chunk_id, chunk_index, chunk_text
                        FROM repository_chunks
                        WHERE repo_id = %s
                        ORDER BY chunk_index ASC, chunk_id ASC
                        LIMIT %s
                        """,
                        (repo_id, evidence_limit),
                    )
                    evidence_rows = await cursor.fetchall()

                    note_rows: list[dict[str, Any]] = []
                    if project["saved_project_id"] is not None:
                        await cursor.execute(
                            """
                            SELECT note_id, note_text, created_at
                            FROM project_notes
                            WHERE saved_project_id = %s
                            ORDER BY created_at DESC, note_id ASC
                            LIMIT 10
                            """,
                            (project["saved_project_id"],),
                        )
                        note_rows = await cursor.fetchall()
        except psycopg.Error as exc:
            raise ProjectToolsRepositoryError("Unable to retrieve project details") from exc

        return self._details_from_rows(project, evidence_rows, note_rows)

    async def save_project(
        self,
        user_key: str,
        repo_id: int,
        saved_project_id: UUID,
        saved_at: datetime,
    ) -> SavedProjectRecord | None:
        try:
            async with self._database.connection() as connection:
                async with connection.transaction():
                    async with connection.cursor(row_factory=dict_row) as cursor:
                        await cursor.execute(
                            "SELECT repo_id FROM repositories WHERE repo_id = %s",
                            (repo_id,),
                        )
                        if await cursor.fetchone() is None:
                            return None
                        await cursor.execute(
                            """
                            INSERT INTO saved_projects (
                                saved_project_id, user_key, repo_id, status,
                                saved_at, updated_at
                            ) VALUES (%s, %s, %s, 'INTERESTED', %s, %s)
                            ON CONFLICT (user_key, repo_id) DO NOTHING
                            """,
                            (saved_project_id, user_key, repo_id, saved_at, saved_at),
                        )
                        await cursor.execute(
                            """
                            SELECT saved_project_id, repo_id, status, saved_at, updated_at
                            FROM saved_projects
                            WHERE user_key = %s AND repo_id = %s
                            """,
                            (user_key, repo_id),
                        )
                        row = await cursor.fetchone()
        except psycopg.Error as exc:
            raise ProjectToolsRepositoryError("Unable to save project") from exc
        if row is None:
            raise ProjectToolsRepositoryError("Saved project persistence returned no result")
        return self._saved_project_from_row(row)

    async def update_project_status(
        self,
        user_key: str,
        repo_id: int,
        project_status: ProjectStatus,
        updated_at: datetime,
    ) -> SavedProjectRecord | None:
        try:
            async with self._database.connection() as connection:
                async with connection.cursor(row_factory=dict_row) as cursor:
                    await cursor.execute(
                        """
                        UPDATE saved_projects
                        SET status = %s, updated_at = %s
                        WHERE user_key = %s AND repo_id = %s
                        RETURNING saved_project_id, repo_id, status, saved_at, updated_at
                        """,
                        (project_status.value, updated_at, user_key, repo_id),
                    )
                    row = await cursor.fetchone()
        except psycopg.Error as exc:
            raise ProjectToolsRepositoryError("Unable to update project status") from exc
        return self._saved_project_from_row(row) if row is not None else None

    async def add_project_note(
        self,
        user_key: str,
        repo_id: int,
        note_id: UUID,
        note: str,
        created_at: datetime,
    ) -> ProjectNoteRecord | None:
        try:
            async with self._database.connection() as connection:
                async with connection.cursor(row_factory=dict_row) as cursor:
                    await cursor.execute(
                        """
                        INSERT INTO project_notes (
                            note_id, saved_project_id, note_text, created_at
                        )
                        SELECT %s, saved_project_id, %s, %s
                        FROM saved_projects
                        WHERE user_key = %s AND repo_id = %s
                        RETURNING note_id, note_text, created_at
                        """,
                        (note_id, note, created_at, user_key, repo_id),
                    )
                    row = await cursor.fetchone()
        except psycopg.Error as exc:
            raise ProjectToolsRepositoryError("Unable to add project note") from exc
        return self._note_from_row(row) if row is not None else None

    @classmethod
    def _details_from_rows(
        cls,
        project: dict[str, Any],
        evidence_rows: list[dict[str, Any]],
        note_rows: list[dict[str, Any]],
    ) -> ProjectDetailsRecord:
        saved_project = None
        if project["saved_project_id"] is not None:
            saved_project = cls._saved_project_from_row(
                {
                    "saved_project_id": project["saved_project_id"],
                    "repo_id": project["repo_id"],
                    "status": project["saved_status"],
                    "saved_at": project["saved_at"],
                    "updated_at": project["updated_at"],
                }
            )
        return ProjectDetailsRecord(
            repo_id=project["repo_id"],
            name=project["name"],
            full_name=project["full_name"],
            owner=project["owner"],
            description=project["description"],
            html_url=project["html_url"],
            primary_language=project["primary_language"],
            stars=project["stars"],
            forks=project["forks"],
            open_issues=project["open_issues"],
            topics=list(project["topics"]),
            license=project["license"],
            evidence=[
                ProjectEvidenceRecord(
                    chunk_id=row["chunk_id"],
                    chunk_index=row["chunk_index"],
                    chunk_text=row["chunk_text"],
                )
                for row in evidence_rows
            ],
            saved_project=saved_project,
            notes=[cls._note_from_row(row) for row in note_rows],
        )

    @staticmethod
    def _saved_project_from_row(row: dict[str, Any]) -> SavedProjectRecord:
        return SavedProjectRecord(
            saved_project_id=row["saved_project_id"],
            repo_id=row["repo_id"],
            status=ProjectStatus(row["status"]),
            saved_at=row["saved_at"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _note_from_row(row: dict[str, Any]) -> ProjectNoteRecord:
        return ProjectNoteRecord(
            note_id=row["note_id"],
            note=row["note_text"],
            created_at=row["created_at"],
        )
