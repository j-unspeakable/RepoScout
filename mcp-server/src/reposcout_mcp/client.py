from collections.abc import Callable, Mapping
from typing import Any, Protocol, cast

import httpx

from reposcout_mcp.config import McpSettings


class WorkspaceConfigProtocol(Protocol):
    def authenticate(self) -> Mapping[str, str]: ...


class WorkspaceAppsProtocol(Protocol):
    def get(self, name: str) -> Any: ...


class WorkspaceClientProtocol(Protocol):
    @property
    def apps(self) -> WorkspaceAppsProtocol: ...

    @property
    def config(self) -> WorkspaceConfigProtocol: ...


class RepoScoutClientError(RuntimeError):
    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable


class RepoScoutClient:
    def __init__(
        self,
        settings: McpSettings,
        *,
        http_client: httpx.Client | None = None,
        workspace_factory: Callable[[], WorkspaceClientProtocol] | None = None,
    ) -> None:
        self._settings = settings
        self._http = http_client or httpx.Client(
            timeout=httpx.Timeout(settings.reposcout_api_timeout_seconds)
        )
        self._owns_http_client = http_client is None
        self._workspace_factory = workspace_factory or self._default_workspace_factory
        self._workspace: WorkspaceClientProtocol | None = None
        self._resolved_url: str | None = settings.reposcout_api_app_url

    def close(self) -> None:
        if self._owns_http_client:
            self._http.close()

    def search_projects(
        self,
        query: str,
        top_k: int = 5,
        language: str | None = None,
        minimum_stars: int | None = None,
    ) -> dict[str, Any]:
        filters: dict[str, Any] = {}
        if language is not None:
            filters["language"] = language
        if minimum_stars is not None:
            filters["minimum_stars"] = minimum_stars
        payload: dict[str, Any] = {"query": query, "top_k": top_k}
        if filters:
            payload["filters"] = filters
        return self._request("POST", "api/tools/search-projects", json=payload)

    def get_project_details(self, repo_id: int, evidence_limit: int = 3) -> dict[str, Any]:
        return self._request(
            "GET",
            f"api/tools/projects/{repo_id}",
            params={"evidence_limit": evidence_limit},
        )

    def save_project(self, repo_id: int) -> dict[str, Any]:
        return self._request("PUT", f"api/tools/saved-projects/{repo_id}")

    def update_project_status(self, repo_id: int, status: str) -> dict[str, Any]:
        return self._request(
            "PATCH",
            f"api/tools/saved-projects/{repo_id}/status",
            json={"status": status},
        )

    def add_project_note(self, repo_id: int, note: str) -> dict[str, Any]:
        return self._request(
            "POST",
            f"api/tools/saved-projects/{repo_id}/notes",
            json={"note": note},
        )

    def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        base_url, headers = self._resolve_target()
        url = f"{base_url}/{path}"
        try:
            response = self._http.request(method, url, headers=headers, **kwargs)
        except httpx.TimeoutException as exc:
            raise RepoScoutClientError(
                "timeout",
                "RepoScout did not respond before the request timed out",
                retryable=True,
            ) from exc
        except httpx.HTTPError as exc:
            raise RepoScoutClientError(
                "unavailable",
                "RepoScout is temporarily unavailable",
                retryable=True,
            ) from exc

        if response.status_code >= 400:
            raise self._response_error(response.status_code)
        try:
            payload = response.json()
        except ValueError as exc:
            raise RepoScoutClientError(
                "invalid_response", "RepoScout returned an invalid response"
            ) from exc
        if not isinstance(payload, dict):
            raise RepoScoutClientError("invalid_response", "RepoScout returned an invalid response")
        return payload

    def _resolve_target(self) -> tuple[str, dict[str, str]]:
        if self._settings.reposcout_api_app_url:
            return self._settings.reposcout_api_app_url, {}

        workspace = self._get_workspace()
        if self._resolved_url is None:
            try:
                app = workspace.apps.get(self._settings.reposcout_app_name or "")
                app_url = getattr(app, "url", None)
            except Exception as exc:
                raise RepoScoutClientError(
                    "configuration", "Unable to resolve the RepoScout application"
                ) from exc
            if not isinstance(app_url, str) or not app_url:
                raise RepoScoutClientError(
                    "configuration", "The RepoScout application has no serving URL"
                )
            self._resolved_url = app_url.rstrip("/")
        try:
            headers = dict(workspace.config.authenticate())
        except Exception as exc:
            raise RepoScoutClientError(
                "authentication", "Unable to authenticate to RepoScout"
            ) from exc
        return self._resolved_url, headers

    def _get_workspace(self) -> WorkspaceClientProtocol:
        if self._workspace is None:
            try:
                self._workspace = self._workspace_factory()
            except Exception as exc:
                raise RepoScoutClientError(
                    "configuration", "Unable to initialize Databricks authentication"
                ) from exc
        return self._workspace

    @staticmethod
    def _default_workspace_factory() -> WorkspaceClientProtocol:
        from databricks.sdk import WorkspaceClient

        return cast(WorkspaceClientProtocol, WorkspaceClient())

    @staticmethod
    def _response_error(status_code: int) -> RepoScoutClientError:
        if status_code in {400, 422}:
            return RepoScoutClientError("invalid_request", "RepoScout rejected the request")
        if status_code == 404:
            return RepoScoutClientError("not_found", "The requested project was not found")
        if status_code in {401, 403}:
            return RepoScoutClientError("authentication", "RepoScout authorization failed")
        if status_code in {429, 502, 503, 504}:
            return RepoScoutClientError(
                "unavailable", "RepoScout is temporarily unavailable", retryable=True
            )
        return RepoScoutClientError("request_failed", "RepoScout request failed")
