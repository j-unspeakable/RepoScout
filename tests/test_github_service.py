import hashlib
from datetime import UTC, datetime

import httpx
import pytest
from pydantic import SecretStr

from app.config import AppEnvironment, Settings
from app.services.github import (
    GitHubRateLimitError,
    GitHubService,
    ReadmeRetrievalStatus,
    RepositoryData,
)


def _repository_payload(repo_id: int, name: str) -> dict[str, object]:
    return {
        "id": repo_id,
        "name": name,
        "full_name": f"owner/{name}",
        "owner": {"login": "owner"},
        "description": f"Description for {name}",
        "html_url": f"https://github.com/owner/{name}",
        "language": "Python",
        "stargazers_count": 42,
        "forks_count": 5,
        "open_issues_count": 3,
        "topics": ["fastapi", "python"],
        "license": {"spdx_id": "MIT", "name": "MIT License"},
        "created_at": "2024-01-01T00:00:00Z",
        "updated_at": "2025-01-01T00:00:00Z",
        "pushed_at": "2025-01-02T00:00:00Z",
    }


def _repository(repo_id: int, name: str) -> RepositoryData:
    now = datetime.now(UTC)
    return RepositoryData(
        repo_id=repo_id,
        name=name,
        full_name=f"owner/{name}",
        owner="owner",
        description=None,
        html_url=f"https://github.com/owner/{name}",
        primary_language="Python",
        stars=1,
        forks=0,
        open_issues=0,
        topics=[],
        license=None,
        created_at=now,
        updated_at=now,
        pushed_at=now,
        ingested_at=now,
    )


def _settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "app_env": AppEnvironment.TEST,
        "github_token": SecretStr("github-secret"),
        "github_retry_attempts": 0,
    }
    values.update(overrides)
    return Settings.model_validate(values)


@pytest.mark.asyncio
async def test_search_uses_best_match_and_maps_metadata() -> None:
    requests: list[httpx.Request] = []
    payload = _repository_payload(1, "repo-one")
    payload["pushed_at"] = None

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={"items": [payload]},
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="https://api.github.test",
        headers={
            "Authorization": "Bearer github-secret",
            "X-GitHub-Api-Version": "2026-03-10",
        },
    ) as client:
        service = GitHubService(_settings(), client=client)
        repositories = await service.search_repositories("fastapi", 30)

    assert len(repositories) == 1
    assert repositories[0].repo_id == 1
    assert repositories[0].stars == 42
    assert repositories[0].license == "MIT"
    assert repositories[0].pushed_at is None
    assert requests[0].url.params["q"] == "fastapi"
    assert requests[0].url.params["per_page"] == "30"
    assert "sort" not in requests[0].url.params
    assert requests[0].headers["Authorization"] == "Bearer github-secret"


@pytest.mark.asyncio
async def test_readmes_record_available_missing_and_error_without_raising() -> None:
    attempts: dict[str, int] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        name = request.url.path.split("/")[3]
        attempts[name] = attempts.get(name, 0) + 1
        if name == "available":
            assert request.headers["Accept"] == "application/vnd.github.raw+json"
            return httpx.Response(200, text="# Available\n")
        if name == "missing":
            return httpx.Response(404)
        return httpx.Response(503)

    async def no_sleep(_: float) -> None:
        return None

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, base_url="https://api.github.test") as client:
        service = GitHubService(_settings(github_retry_attempts=1), client=client, sleep=no_sleep)
        results = await service.retrieve_readmes(
            [
                _repository(1, "available"),
                _repository(2, "missing"),
                _repository(3, "error"),
            ]
        )

    readmes = {result.repository.name: result.readme for result in results}
    assert readmes["available"].retrieval_status is ReadmeRetrievalStatus.AVAILABLE
    assert readmes["available"].content_hash == hashlib.sha256(b"# Available\n").hexdigest()
    assert readmes["missing"].retrieval_status is ReadmeRetrievalStatus.MISSING
    assert readmes["missing"].raw_content is None
    assert readmes["error"].retrieval_status is ReadmeRetrievalStatus.ERROR
    assert attempts["error"] == 2


@pytest.mark.asyncio
async def test_search_rate_limit_exposes_safe_retry_time() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"Retry-After": "12"})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, base_url="https://api.github.test") as client:
        service = GitHubService(_settings(), client=client)
        with pytest.raises(GitHubRateLimitError) as error:
            await service.search_repositories("fastapi", 30)

    assert error.value.retry_after == 12
    assert "github-secret" not in str(error.value)


@pytest.mark.asyncio
async def test_transient_search_failure_retries_then_succeeds() -> None:
    attempts = 0
    sleeps: list[float] = []

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(503)
        return httpx.Response(200, json={"items": [_repository_payload(1, "repo-one")]})

    async def record_sleep(delay: float) -> None:
        sleeps.append(delay)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, base_url="https://api.github.test") as client:
        service = GitHubService(
            _settings(github_retry_attempts=1), client=client, sleep=record_sleep
        )
        repositories = await service.search_repositories("fastapi", 1)

    assert len(repositories) == 1
    assert attempts == 2
    assert sleeps == [1.0]
