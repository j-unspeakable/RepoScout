from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any, cast

import httpx
import pytest

from app.config import AppEnvironment, Settings
from app.database.credentials import LakebaseCredentialProvider
from app.database.pool import LakebasePool
from app.main import create_app


def _local_settings() -> Settings:
    return Settings(
        app_env=AppEnvironment.LOCAL,
        databricks_config_profile="reposcout",
        github_token="github-secret",
        pghost="lakebase.example",
        pgdatabase="reposcout",
        pguser="developer@example.com",
        lakebase_endpoint="projects/p/branches/b/endpoints/e",
    )


class FakePostgresApi:
    def __init__(self) -> None:
        self.calls = 0

    def generate_database_credential(self, *, endpoint: str) -> SimpleNamespace:
        self.calls += 1
        assert endpoint == "projects/p/branches/b/endpoints/e"
        return SimpleNamespace(token=f"database-secret-{self.calls}")


@pytest.mark.asyncio
async def test_sdk_credential_generation_uses_asyncify_bridge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    postgres_api = FakePostgresApi()
    workspace = SimpleNamespace(postgres=postgres_api)
    bridged_functions: list[object] = []

    def fake_asyncify(function):
        bridged_functions.append(function)

        async def call():
            return function()

        return call

    monkeypatch.setattr("app.database.credentials.asyncify", fake_asyncify)
    provider = LakebaseCredentialProvider(
        "projects/p/branches/b/endpoints/e",
        workspace_client=workspace,
    )

    token = await provider.get_credential()

    assert token == "database-secret-1"
    assert bridged_functions == [provider.get_credential_sync]


@pytest.mark.asyncio
async def test_pool_is_closed_by_default_and_generates_per_connection_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakePool:
        def __init__(self, **kwargs: Any) -> None:
            self.kwargs = kwargs
            self.open_calls: list[bool] = []
            self.close_calls = 0

        async def open(self, *, wait: bool) -> None:
            self.open_calls.append(wait)

        async def close(self) -> None:
            self.close_calls += 1

        @asynccontextmanager
        async def connection(self):
            yield object()

    class FakeCredentialProvider:
        def __init__(self) -> None:
            self.calls = 0

        async def get_credential(self) -> str:
            self.calls += 1
            return f"database-secret-{self.calls}"

    monkeypatch.setattr("app.database.pool.AsyncConnectionPool", FakePool)
    credentials = FakeCredentialProvider()
    database = LakebasePool(_local_settings(), credentials)
    fake_pool = cast(Any, database.pool)

    assert fake_pool.kwargs["open"] is False
    await database.open()
    first = await fake_pool.kwargs["kwargs"]()
    second = await fake_pool.kwargs["kwargs"]()
    await database.close()

    assert fake_pool.open_calls == [True]
    assert fake_pool.close_calls == 1
    assert first["password"] == "database-secret-1"
    assert second["password"] == "database-secret-2"
    assert credentials.calls == 2
    assert "database-secret" not in repr(_local_settings())


@pytest.mark.asyncio
async def test_fastapi_lifespan_explicitly_opens_and_closes_pool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []

    class FakeCredentialProvider:
        def __init__(self, endpoint: str, profile: str | None = None) -> None:
            assert endpoint == "projects/p/branches/b/endpoints/e"
            assert profile == "reposcout"

    class FakeDatabase:
        def __init__(self, settings: Settings, credential_provider: object) -> None:
            assert settings.app_env is AppEnvironment.LOCAL

        async def open(self) -> None:
            events.append("open")

        async def close(self) -> None:
            events.append("close")

    class FakeGitHub:
        def __init__(self, settings: Settings) -> None:
            pass

        async def close(self) -> None:
            events.append("github-close")

    monkeypatch.setattr("app.main.LakebaseCredentialProvider", FakeCredentialProvider)
    monkeypatch.setattr("app.main.LakebasePool", FakeDatabase)
    monkeypatch.setattr("app.main.GitHubService", FakeGitHub)

    application = create_app(_local_settings())
    async with application.router.lifespan_context(application):
        assert events == ["open"]
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=application), base_url="http://testserver"
        ) as client:
            assert (await client.get("/health")).status_code == 200

    assert events == ["open", "github-close", "close"]


def test_generated_database_token_is_not_part_of_static_settings() -> None:
    settings = _local_settings()

    assert not hasattr(settings, "password")
    assert not hasattr(settings, "database_credential")
    assert "github-secret" not in repr(settings)


def test_named_profile_is_passed_to_workspace_client(monkeypatch: pytest.MonkeyPatch) -> None:
    captured_profiles: list[str | None] = []

    class FakeWorkspace:
        postgres = FakePostgresApi()

    def workspace_client(*, profile: str | None = None) -> FakeWorkspace:
        captured_profiles.append(profile)
        return FakeWorkspace()

    monkeypatch.setattr("app.database.credentials.WorkspaceClient", workspace_client)

    LakebaseCredentialProvider(
        "projects/p/branches/b/endpoints/e",
        profile="reposcout",
    )

    assert captured_profiles == ["reposcout"]
