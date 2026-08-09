from types import SimpleNamespace

import httpx
import pytest

from reposcout_mcp.client import RepoScoutClient, RepoScoutClientError
from reposcout_mcp.config import McpSettings


class FakeApps:
    def __init__(self, calls: list[tuple[str, ...]]) -> None:
        self.calls = calls

    def get(self, name: str) -> SimpleNamespace:
        self.calls.append(("get", name))
        return SimpleNamespace(url="https://reposcout.example/")


class FakeConfig:
    def __init__(self, calls: list[tuple[str, ...]]) -> None:
        self.calls = calls

    def authenticate(self) -> dict[str, str]:
        self.calls.append(("authenticate",))
        return {"Authorization": "Bearer generated-by-sdk"}


class FakeWorkspace:
    def __init__(self, calls: list[tuple[str, ...]]) -> None:
        self.apps = FakeApps(calls)
        self.config = FakeConfig(calls)


def test_local_url_mode_uses_no_authentication_and_correct_contracts() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"ok": True})

    client = RepoScoutClient(
        McpSettings(reposcout_api_app_url="http://127.0.0.1:8000/"),
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        workspace_factory=lambda: pytest.fail("workspace must not be used locally"),
    )

    client.search_projects("pipelines", 3, "Python", 100)
    client.get_project_details(42, 2)
    client.save_project(42)
    client.update_project_status(42, "IN_PROGRESS")
    client.add_project_note(42, "Try this")

    assert [request.url.path for request in requests] == [
        "/api/tools/search-projects",
        "/api/tools/projects/42",
        "/api/tools/saved-projects/42",
        "/api/tools/saved-projects/42/status",
        "/api/tools/saved-projects/42/notes",
    ]
    assert all("Authorization" not in request.headers for request in requests)


def test_deployed_mode_caches_url_but_refreshes_authentication() -> None:
    calls: list[tuple[str, ...]] = []
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"ok": True})

    client = RepoScoutClient(
        McpSettings(reposcout_app_name="reposcout"),
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        workspace_factory=lambda: FakeWorkspace(calls),
    )

    client.save_project(1)
    client.save_project(2)

    assert calls == [("get", "reposcout"), ("authenticate",), ("authenticate",)]
    assert all(
        request.headers["Authorization"] == "Bearer generated-by-sdk" for request in requests
    )


@pytest.mark.parametrize(
    ("status", "code", "retryable"),
    [(404, "not_found", False), (422, "invalid_request", False), (503, "unavailable", True)],
)
def test_http_failures_are_safe(status: int, code: str, retryable: bool) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"detail": "database secret"})

    client = RepoScoutClient(
        McpSettings(reposcout_api_app_url="http://local"),
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    with pytest.raises(RepoScoutClientError) as caught:
        client.save_project(42)

    assert caught.value.code == code
    assert caught.value.retryable is retryable
    assert "database secret" not in str(caught.value)
