from dataclasses import dataclass
from typing import Any, Protocol

import psycopg
from psycopg import sql
from psycopg.rows import dict_row

from app.database.pool import ConnectionProvider


class SearchRepositoryError(RuntimeError):
    """A safe vector-search database failure."""


@dataclass(frozen=True, slots=True)
class SearchCandidate:
    chunk_id: str
    chunk_index: int
    chunk_text: str
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
    cosine_distance: float


class SearchRepositoryProtocol(Protocol):
    async def search_chunks(
        self,
        embedding: list[float],
        embedding_model: str,
        language: str | None,
        minimum_stars: int | None,
        candidate_limit: int,
    ) -> list[SearchCandidate]: ...


class SearchRepository:
    def __init__(self, database: ConnectionProvider) -> None:
        self._database = database

    async def search_chunks(
        self,
        embedding: list[float],
        embedding_model: str,
        language: str | None,
        minimum_stars: int | None,
        candidate_limit: int,
    ) -> list[SearchCandidate]:
        conditions = [sql.SQL("c.embedding_model = %(embedding_model)s")]
        parameters: dict[str, object] = {
            "embedding": self._vector_literal(embedding),
            "embedding_model": embedding_model,
            "candidate_limit": candidate_limit,
        }
        if language is not None:
            conditions.append(sql.SQL("lower(r.primary_language) = lower(%(language)s)"))
            parameters["language"] = language
        if minimum_stars is not None:
            conditions.append(sql.SQL("r.stars >= %(minimum_stars)s"))
            parameters["minimum_stars"] = minimum_stars

        query = sql.SQL(
            """
            SELECT c.chunk_id, c.chunk_index, c.chunk_text,
                   r.repo_id, r.name, r.full_name, r.owner, r.description,
                   r.html_url, r.primary_language, r.stars, r.forks,
                   r.open_issues, r.topics, r.license,
                   c.embedding <=> %(embedding)s::vector AS cosine_distance
            FROM repository_chunks AS c
            JOIN repositories AS r ON r.repo_id = c.repo_id
            WHERE {conditions}
            ORDER BY c.embedding <=> %(embedding)s::vector ASC
            LIMIT %(candidate_limit)s
            """
        ).format(conditions=sql.SQL(" AND ").join(conditions))

        try:
            async with self._database.connection() as connection:
                async with connection.cursor(row_factory=dict_row) as cursor:
                    await cursor.execute(query, parameters)
                    rows = await cursor.fetchall()
        except psycopg.Error as exc:
            raise SearchRepositoryError("Unable to search repository chunks") from exc

        return [self._candidate_from_row(row) for row in rows]

    @staticmethod
    def _vector_literal(embedding: list[float]) -> str:
        return "[" + ",".join(format(value, ".9g") for value in embedding) + "]"

    @staticmethod
    def _candidate_from_row(row: dict[str, Any]) -> SearchCandidate:
        return SearchCandidate(
            chunk_id=row["chunk_id"],
            chunk_index=row["chunk_index"],
            chunk_text=row["chunk_text"],
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
            cosine_distance=float(row["cosine_distance"]),
        )
