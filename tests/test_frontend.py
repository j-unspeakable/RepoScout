import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import anyio
import httpx
import pytest

from app.config import AppEnvironment, Settings
from app.main import create_app

PROJECT_ROOT = Path(__file__).parents[1]
FRONTEND_ROOT = PROJECT_ROOT / "frontend"


@asynccontextmanager
async def _client() -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(Settings(app_env=AppEnvironment.TEST))
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            yield client


@pytest.mark.asyncio
async def test_frontend_files_and_api_routes_remain_available(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert (FRONTEND_ROOT / "index.html").is_file()
    assert (FRONTEND_ROOT / "assets" / "styles.css").is_file()
    assert (FRONTEND_ROOT / "assets" / "app.js").is_file()
    assert (FRONTEND_ROOT / "assets" / "favicon.svg").is_file()

    async def run_sync_inline(function: Any, *args: Any, **_kwargs: Any) -> Any:
        return function(*args)

    monkeypatch.setattr(anyio.to_thread, "run_sync", run_sync_inline)

    async with _client() as client:
        index = await client.get("/")
        index_head = await client.head("/")
        stylesheet = await client.get("/assets/styles.css")
        favicon = await client.get("/assets/favicon.svg")
        script_head = await client.head("/assets/app.js")
        missing = await client.get("/not-a-client-route")
        docs = await client.get("/docs")
        schema = await client.get("/openapi.json")
        health = await client.get("/health")

    assert index.status_code == 200
    assert "RepoScout" in index.text
    assert index_head.status_code == 200
    assert index_head.content == b""
    assert stylesheet.status_code == 200
    assert "prefers-reduced-motion" in stylesheet.text
    assert favicon.status_code == 200
    assert favicon.headers["content-type"].startswith("image/svg+xml")
    assert script_head.status_code == 200
    assert script_head.content == b""
    assert missing.status_code == 404
    assert docs.status_code == 200
    assert schema.status_code == 200
    assert "/corpus/summary" in schema.json()["paths"]
    assert "/indexing-requests" in schema.json()["paths"]
    assert health.json() == {"status": "ok", "environment": "test"}


def test_frontend_uses_one_proxy_safe_application_base() -> None:
    page = (FRONTEND_ROOT / "index.html").read_text()
    script = (FRONTEND_ROOT / "assets" / "app.js").read_text()
    main_source = (PROJECT_ROOT / "app" / "main.py").read_text()

    assert script.count('const APPLICATION_BASE_URL = new URL("./", document.baseURI);') == 1
    assert "return new URL(relativePath, APPLICATION_BASE_URL);" in script
    assert "fetch(apiUrl(relativePath)" in script
    assert script.count("fetch(") == 1
    assert 'apiRequest("corpus/summary")' in script
    assert 'apiRequest("indexing-requests"' in script
    assert 'endpoint: "search/semantic"' in script
    assert 'endpoint: "search/ask"' in script
    assert 'application.frontend("/", directory=FRONTEND_DIRECTORY, fallback=None)' in main_source

    forbidden = ("localhost", "127.0.0.1", "databricksapps.com", 'fetch("/', "fetch('/")
    for value in forbidden:
        assert value not in page
        assert value not in script
    assert 'href="/' not in page
    assert 'src="/' not in page
    assert '<link rel="icon" href="./assets/favicon.svg" type="image/svg+xml">' in page


def test_frontend_contract_is_safe_accessible_and_self_contained() -> None:
    page = (FRONTEND_ROOT / "index.html").read_text()
    styles = (FRONTEND_ROOT / "assets" / "styles.css").read_text()
    script = (FRONTEND_ROOT / "assets" / "app.js").read_text()

    assert "innerHTML" not in script
    assert "insertAdjacentHTML" not in script
    assert re.search(r"\son[a-z]+\s*=", page, flags=re.IGNORECASE) is None
    assert "https://" not in page
    assert "http://" not in page
    assert 'aria-live="polite"' in page
    assert 'aria-label="Primary navigation"' in page
    assert 'class="cancel-button"' in page
    assert "Searching repositories…" in script
    assert "This may take several seconds…" in script
    assert "AbortController" in script
    assert "noopener noreferrer" in script
    assert 'url.hostname.toLowerCase() === "github.com"' in script
    assert "@media (prefers-reduced-motion: reduce)" in styles
    assert "animation: spin" in styles
    assert "animation: skeleton-pulse" in styles
    assert "#07110f" in styles
    assert "#2dd4bf" in styles
    assert page.count('data-nav-view="') == 2
    assert "data-mode-view" not in page
    assert "mode-switcher" not in page
    assert "mode-switcher" not in styles
    assert page.count('class="example-query"') == 3
    assert "Learn RAG with Python" in page
    assert "Spark data pipelines" in page
    assert "AI agents with databases" in page
    assert "setupExampleQueries" in script
    assert "query.value = button.textContent.trim()" in script
    assert 'id="indexing-request-form"' in page
    assert 'id="coverage-toggle"' in page
    assert 'id="coverage-panel"' in page
    assert "What were you hoping to find?" in page
    assert "Request more coverage" in page
    assert "suggested_repository_url" not in page
    assert "suggested_repository_url" not in script
    assert 'apiRequest("ingestions"' not in script
    assert "Request received for review." in script
    assert "status.dataset.completed" in script
    assert "setTimeout" not in script
    assert "corpusReadinessCopy" in script
    assert "summary.not_searchable_reasons" in script
    assert "awaiting indexing" in script
    assert "have README content available" in script
    assert "could not currently be retrieved" in script
    assert "retrieval_status" not in script
