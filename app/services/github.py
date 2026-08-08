import asyncio
import hashlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Protocol

import httpx

from app.config import Settings


class ReadmeRetrievalStatus(StrEnum):
    AVAILABLE = "available"
    MISSING = "missing"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class RepositoryData:
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
    created_at: datetime
    updated_at: datetime
    pushed_at: datetime | None
    ingested_at: datetime


@dataclass(frozen=True, slots=True)
class ReadmeData:
    repo_id: int
    raw_content: str | None
    content_hash: str | None
    retrieved_at: datetime
    retrieval_status: ReadmeRetrievalStatus


@dataclass(frozen=True, slots=True)
class RepositoryWithReadme:
    repository: RepositoryData
    readme: ReadmeData


class GitHubError(RuntimeError):
    """Base class for safe-to-report GitHub integration failures."""


class GitHubSearchError(GitHubError):
    pass


class GitHubRateLimitError(GitHubError):
    def __init__(self, message: str, retry_after: int | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class GitHubServiceProtocol(Protocol):
    async def search_repositories(
        self, search_query: str, max_repositories: int
    ) -> list[RepositoryData]: ...

    async def retrieve_readmes(
        self, repositories: list[RepositoryData]
    ) -> list[RepositoryWithReadme]: ...


SleepCallable = Callable[[float], Awaitable[None]]


class GitHubService:
    def __init__(
        self,
        settings: Settings,
        client: httpx.AsyncClient | None = None,
        sleep: SleepCallable = asyncio.sleep,
    ) -> None:
        self._settings = settings
        self._sleep = sleep
        self._owns_client = client is None
        token = settings.github_token.get_secret_value() if settings.github_token else None
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": settings.github_api_version,
            "User-Agent": "RepoScout/0.1",
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self._client = client or httpx.AsyncClient(
            base_url=settings.github_api_url,
            headers=headers,
            timeout=httpx.Timeout(settings.github_timeout_seconds, connect=5.0),
        )

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def search_repositories(
        self, search_query: str, max_repositories: int
    ) -> list[RepositoryData]:
        repositories: list[RepositoryData] = []
        page = 1
        ingested_at = datetime.now(UTC)

        while len(repositories) < max_repositories:
            per_page = min(100, max_repositories - len(repositories))
            try:
                response = await self._request(
                    "GET",
                    "/search/repositories",
                    params={"q": search_query, "per_page": per_page, "page": page},
                )
                if response is None:
                    raise GitHubSearchError("GitHub repository search returned no response")
                payload = response.json()
                items = payload["items"]
                if not isinstance(items, list):
                    raise TypeError("items is not a list")
                repositories.extend(self._map_repository(item, ingested_at) for item in items)
            except GitHubRateLimitError:
                raise
            except (GitHubError, AttributeError, KeyError, TypeError, ValueError) as exc:
                if isinstance(exc, GitHubSearchError):
                    raise
                raise GitHubSearchError("GitHub repository search failed") from exc

            if not items or len(items) < per_page:
                break
            page += 1

        return repositories[:max_repositories]

    async def retrieve_readmes(
        self, repositories: list[RepositoryData]
    ) -> list[RepositoryWithReadme]:
        semaphore = asyncio.Semaphore(self._settings.github_readme_concurrency)

        async def retrieve(repository: RepositoryData) -> RepositoryWithReadme:
            async with semaphore:
                readme = await self._retrieve_readme(repository)
                return RepositoryWithReadme(repository=repository, readme=readme)

        return list(await asyncio.gather(*(retrieve(repository) for repository in repositories)))

    async def _retrieve_readme(self, repository: RepositoryData) -> ReadmeData:
        retrieved_at = datetime.now(UTC)
        try:
            response = await self._request(
                "GET",
                f"/repos/{repository.owner}/{repository.name}/readme",
                headers={"Accept": "application/vnd.github.raw+json"},
                allow_not_found=True,
            )
        except GitHubError:
            return ReadmeData(
                repo_id=repository.repo_id,
                raw_content=None,
                content_hash=None,
                retrieved_at=retrieved_at,
                retrieval_status=ReadmeRetrievalStatus.ERROR,
            )

        if response is None:
            return ReadmeData(
                repo_id=repository.repo_id,
                raw_content=None,
                content_hash=None,
                retrieved_at=retrieved_at,
                retrieval_status=ReadmeRetrievalStatus.MISSING,
            )

        content = response.text
        return ReadmeData(
            repo_id=repository.repo_id,
            raw_content=content,
            content_hash=hashlib.sha256(content.encode("utf-8")).hexdigest(),
            retrieved_at=retrieved_at,
            retrieval_status=ReadmeRetrievalStatus.AVAILABLE,
        )

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        allow_not_found: bool = False,
    ) -> httpx.Response | None:
        attempts = self._settings.github_retry_attempts + 1
        last_error: Exception | None = None

        for attempt in range(attempts):
            try:
                response = await self._client.request(method, path, params=params, headers=headers)
            except httpx.RequestError as exc:
                last_error = exc
                if attempt + 1 < attempts:
                    await self._sleep(2**attempt)
                    continue
                raise GitHubError("GitHub request failed after retries") from exc

            if allow_not_found and response.status_code == httpx.codes.NOT_FOUND:
                return None

            if self._is_rate_limited(response):
                retry_after = self._retry_after_seconds(response)
                if attempt + 1 < attempts:
                    await self._sleep(self._retry_delay(retry_after, attempt))
                    continue
                raise GitHubRateLimitError("GitHub rate limit exceeded", retry_after)

            if response.status_code == httpx.codes.TOO_MANY_REQUESTS or 500 <= response.status_code:
                last_error = httpx.HTTPStatusError(
                    "Transient GitHub response",
                    request=response.request,
                    response=response,
                )
                if attempt + 1 < attempts:
                    await self._sleep(
                        self._retry_delay(self._retry_after_seconds(response), attempt)
                    )
                    continue

            try:
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                raise GitHubError(f"GitHub request returned HTTP {response.status_code}") from exc
            return response

        raise GitHubError("GitHub request failed after retries") from last_error

    @staticmethod
    def _is_rate_limited(response: httpx.Response) -> bool:
        return response.status_code == httpx.codes.TOO_MANY_REQUESTS or (
            response.status_code == httpx.codes.FORBIDDEN
            and (
                response.headers.get("X-RateLimit-Remaining") == "0"
                or "rate limit" in response.text.lower()
            )
        )

    @staticmethod
    def _retry_delay(retry_after: int | None, attempt: int) -> float:
        return float(min(retry_after or 2**attempt, 30))

    @staticmethod
    def _retry_after_seconds(response: httpx.Response) -> int | None:
        retry_after = response.headers.get("Retry-After")
        if retry_after and retry_after.isdigit():
            return max(1, int(retry_after))

        reset = response.headers.get("X-RateLimit-Reset")
        if reset and reset.isdigit():
            now = int(datetime.now(UTC).timestamp())
            return max(1, int(reset) - now)
        return None

    @staticmethod
    def _map_repository(item: dict[str, Any], ingested_at: datetime) -> RepositoryData:
        license_data = item.get("license")
        license_name = None
        if isinstance(license_data, dict):
            license_name = license_data.get("spdx_id") or license_data.get("name")

        pushed_at = item.get("pushed_at")
        return RepositoryData(
            repo_id=int(item["id"]),
            name=str(item["name"]),
            full_name=str(item["full_name"]),
            owner=str(item["owner"]["login"]),
            description=item.get("description"),
            html_url=str(item["html_url"]),
            primary_language=item.get("language"),
            stars=int(item["stargazers_count"]),
            forks=int(item["forks_count"]),
            open_issues=int(item["open_issues_count"]),
            topics=[str(topic) for topic in item.get("topics", [])],
            license=license_name,
            created_at=GitHubService._parse_datetime(item["created_at"]),
            updated_at=GitHubService._parse_datetime(item["updated_at"]),
            pushed_at=GitHubService._parse_datetime(pushed_at) if pushed_at else None,
            ingested_at=ingested_at,
        )

    @staticmethod
    def _parse_datetime(value: str) -> datetime:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
