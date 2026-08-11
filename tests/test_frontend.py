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
    assert (FRONTEND_ROOT / "assets" / "theme.js").is_file()
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
        theme_script_head = await client.head("/assets/theme.js")
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
    assert theme_script_head.status_code == 200
    assert theme_script_head.content == b""
    assert missing.status_code == 404
    assert docs.status_code == 200
    assert schema.status_code == 200
    assert "/corpus/summary" in schema.json()["paths"]
    assert "/indexing-requests" in schema.json()["paths"]
    assert "/assistant/messages" in schema.json()["paths"]
    assert "/saved-projects" in schema.json()["paths"]
    assert "delete" in schema.json()["paths"]["/saved-projects/{repo_id}"]
    assert health.json() == {"status": "ok", "environment": "test"}


def test_frontend_uses_one_proxy_safe_application_base() -> None:
    page = (FRONTEND_ROOT / "index.html").read_text()
    script = (FRONTEND_ROOT / "assets" / "app.js").read_text()
    theme_script = (FRONTEND_ROOT / "assets" / "theme.js").read_text()
    main_source = (PROJECT_ROOT / "app" / "main.py").read_text()

    assert script.count('const APPLICATION_BASE_URL = new URL("./", document.baseURI);') == 1
    assert "return new URL(relativePath, APPLICATION_BASE_URL);" in script
    assert "fetch(apiUrl(relativePath), options)" in script
    assert script.count("fetch(") == 1
    assert 'apiRequest("corpus/summary")' in script
    assert 'apiRequest("indexing-requests"' in script
    assert 'apiRequest("search/semantic"' in script
    assert 'apiFetch("assistant/messages/stream"' in script
    assert "assistant/turns/${encodeURIComponent(turnId)}/cancel" in script
    assert 'apiRequest("saved-projects"' in script
    assert "apiRequest(`saved-projects/${encodeURIComponent(current.repoId)}`" in script
    assert 'endpoint: "search/ask"' not in script
    assert "setupDiscoverSearch" in script
    assert "setupSearchMode" not in script
    assert "renderAskResponse" not in script
    assert 'mode === "ask"' not in script
    assert 'application.frontend("/", directory=FRONTEND_DIRECTORY, fallback=None)' in main_source

    forbidden = ("localhost", "127.0.0.1", "databricksapps.com", 'fetch("/', "fetch('/")
    for value in forbidden:
        assert value not in page
        assert value not in script
        assert value not in theme_script
    assert 'href="/' not in page
    assert 'src="/' not in page
    assert '<link rel="icon" href="./assets/favicon.svg" type="image/svg+xml">' in page
    assert '<script src="./assets/theme.js"></script>' in page
    assert page.index('<script src="./assets/theme.js"></script>') < page.index(
        '<link rel="stylesheet" href="./assets/styles.css">'
    )


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
    assert "Working through your request…" in script
    assert "Searching projects…" in script
    assert "Saving projects…" in script
    assert "Adding notes…" in script
    assert "AbortController" in script
    assert "noopener noreferrer" in script
    assert 'url.hostname.toLowerCase() === "github.com"' in script
    assert "@media (prefers-reduced-motion: reduce)" in styles
    assert "animation: spin" in styles
    assert "animation: skeleton-pulse" in styles
    assert "#07110f" in styles
    assert "#2dd4bf" in styles
    assert page.count('data-nav-view="') == 3
    assert 'data-nav-view="projects"' in page
    assert 'id="search-workspace"' in page
    assert 'class="nav-flagship"' in page
    assert 'class="nav-flagship-icon" aria-hidden="true"' in page
    assert "setupPrimaryNavigation" in script
    assert "scrollElementIntoView" in script
    assert "scrollToWorkspace: true" in script
    assert 'scrollElementIntoView(document.querySelector("#search-workspace"))' in script
    assert "window.requestAnimationFrame(() => scrollElementIntoView(results))" in script
    assert ".primary-nav .nav-flagship" in styles
    assert "@keyframes flagship-glint" in styles
    assert "scroll-margin-top: 92px" in styles
    assert "data-mode-view" not in page
    assert "mode-switcher" not in page
    assert "mode-switcher" not in styles
    assert page.count('class="example-query"') == 3
    assert 'id="discover-top-k"' in page
    assert 'name="top_k"' in page
    assert 'min="1"' in page
    assert 'max="10"' in page
    assert 'value="5"' in page
    assert "top_k: Number(rawTopK)" in script
    assert "payload.top_k < 1 || payload.top_k > 10" in script
    assert "const TOP_K" not in script
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
    assert "showRank && Number.isInteger(project.rank)" in script
    assert 'element("span", "rank-badge", String(project.rank))' in script
    assert "`#${project.rank" not in script


def test_frontend_theme_contract_is_accessible_and_consistent() -> None:
    page = (FRONTEND_ROOT / "index.html").read_text()
    styles = (FRONTEND_ROOT / "assets" / "styles.css").read_text()
    theme_script = (FRONTEND_ROOT / "assets" / "theme.js").read_text()

    assert 'id="theme-toggle"' in page
    assert 'aria-label="Switch to light theme"' in page
    assert 'title="Switch to light theme"' in page
    assert "data-theme-icon" in page
    assert '[data-theme="dark"]' in styles
    assert '[data-theme="light"]' in styles
    assert "color-scheme: dark" in styles
    assert "color-scheme: light" in styles
    assert "--page: #07110f" in styles
    assert "--page: #f4f8f7" in styles
    assert ".theme-toggle" in styles
    assert theme_script.count('const THEME_STORAGE_KEY = "reposcout.theme";') == 1
    assert "value === DARK_THEME || value === LIGHT_THEME" in theme_script
    assert 'window.matchMedia("(prefers-color-scheme: dark)")' in theme_script
    assert "readExplicitTheme() ?? preferredSystemTheme()" in theme_script
    assert "window.localStorage.getItem(THEME_STORAGE_KEY)" in theme_script
    assert "window.localStorage.setItem(THEME_STORAGE_KEY, theme)" in theme_script
    assert "if (readExplicitTheme() === null)" in theme_script
    assert 'toggle.setAttribute("aria-label", label)' in theme_script
    assert "toggle.title = label" in theme_script
    assert 'resolvedTheme === DARK_THEME ? "#07110f" : "#f4f8f7"' in theme_script
    assert 'localStorage.setItem(THEME_STORAGE_KEY, "system")' not in theme_script
    assert "event.key === THEME_STORAGE_KEY || event.key === null" in theme_script
    assert theme_script.count("applyTheme(readExplicitTheme() ?? preferredSystemTheme())") == 2


def test_agent_chat_and_my_projects_frontend_contract() -> None:
    page = (FRONTEND_ROOT / "index.html").read_text()
    script = (FRONTEND_ROOT / "assets" / "app.js").read_text()
    styles = (FRONTEND_ROOT / "assets" / "styles.css").read_text()

    assert 'id="ask-transcript"' in page
    assert 'role="log"' in page
    assert 'id="new-conversation"' in page
    assert 'id="chat-restart"' in page
    assert "New chat" in page
    assert "Start new chat to continue" in page
    assert 'class="chat-surface"' in page
    assert 'class="chat-composer-form"' in page
    assert 'rows="1"' in page
    assert 'aria-label="Send message"' in page
    assert 'aria-label="Stop waiting"' in page
    assert "Enter to send · Shift+Enter for a new line" in page
    assert 'id="projects-view"' in page
    assert 'id="projects-refresh"' in page
    assert "ask-language" not in page
    assert "ask-stars" not in page
    assert "sessionStorage" in script
    assert "CHAT_SESSION_KEY" in script
    assert "conversation_id: chatState.conversationId" in script
    assert "response.message.content" in script
    assert "response.message.presentation" in script
    assert "response.message.evidence" in script
    assert "response.output" not in script
    assert "tool_calls" not in script
    assert "reasoning" not in script
    assert "Supervisor" not in page
    assert "MCP" not in page
    assert "UNCERTAIN_COMPLETION_MESSAGE" in script
    assert "CANCELLED_COMPLETION_MESSAGE" in script
    assert "SAFE_CANCELLATION_MESSAGE" in script
    assert "Stopped waiting. If this request included saving a project" in script
    assert "Check My Projects before retrying" in script
    assert "setTimeout" not in script
    assert "typing-indicator" in styles
    assert "chat-progress-copy" in styles
    assert "ASSISTANT_PROGRESS_COPY" in script
    assert 'cancellation?.outcome === "cancelled"' in script
    assert 'cancellation?.outcome === "completed"' in script
    assert "activeController.abort()" in script
    assert "chat-transcript" in styles
    assert "chat-action-slot" in styles
    assert (
        ".chat-stop-button {\n"
        "  border-color: transparent;\n"
        "  color: var(--accent);\n"
        "  background: transparent;\n"
        "}" in styles
    )
    assert "max-height: 160px" in styles
    assert "CHAT_ONBOARDING_MESSAGE" in script
    assert "evidence-based details or comparisons" in script
    assert "Interested, To Try, In Progress, or Completed" in script
    assert "createChatOnboardingMessage" in script
    assert "createChatProjectCards" in script
    assert "createChatProjectReferences" in script
    assert '"Referenced projects"' in script
    assert 'presentation === "cards"' in script
    assert 'presentation === "references"' in script
    assert "alignLatestAssistantStart: true" in script
    assert '".chat-message.is-assistant:not(.is-pending)"' in script
    assert '["cards", "references", "text"]' in script
    assert "createProjectCard(project, citationTargets" in script
    assert script.index("renderAnswerBody(body, message.content") < script.index(
        "body.append(projectCards)"
    )
    assert "showRank: false" in script
    assert "showActionSlot: false" in script
    assert "normalizeAnswerMarkdown" in script
    assert "createChatEvidenceGroup" not in script
    assert "chatEvidenceHost" not in script
    assert "placeChatEvidence" not in script
    assert 'element("summary", "", "Why this matched")' in script
    assert "Supporting README evidence" not in script
    assert "validStoredAssistantEvidenceProject" in script
    assert "chat-project-grid" in styles
    assert "chat-project-references" in styles
    assert "chat-project-reference-link" in styles
    assert (
        ".chat-message.is-assistant:not(.is-onboarding):not(.is-pending) {\n"
        "  width: 100%;\n"
        "  max-width: 100%;"
    ) in styles
    assert ".chat-project-references {\n  display: grid;" in styles
    assert "padding: 12px 3px 3px;" in styles
    assert ".chat-project-reference-list li {\n  min-width: 0;" in styles
    assert ".chat-message-body .chat-project-reference-list li + li {\n  margin-top: 0;" in styles
    assert "overflow-wrap: anywhere" in styles
    assert "chat-evidence-fallback" not in styles
    assert "readmeMatchStrength" in script
    assert "README match: ${strength}" in script
    assert 'return "Strong"' in script
    assert 'return "Moderate"' in script
    assert 'return "Limited"' in script
    assert "Based on semantic similarity between your request and the indexed README." in script
    assert "percentage" not in script.lower()
    assert "probability" not in script.lower()
    assert "confidence" not in script.lower()
    assert "readme-match" in styles
    assert "visibleMessages.length === 0" in script
    assert "fragment.append(createChatOnboardingMessage())" in script
    assert "message: CHAT_ONBOARDING_MESSAGE" not in script
    assert "chatState.messages.push(CHAT_ONBOARDING_MESSAGE" not in script
    assert "event.shiftKey" in script
    assert "event.isComposing" in script
    assert "event.keyCode === 229" in script
    assert "form.requestSubmit()" in script
    assert "Math.min(query.scrollHeight, MAX_CHAT_COMPOSER_HEIGHT)" in script
    assert "restoreDraft(message)" in script
    assert "query.disabled = chatState.blocked" in script
    assert "query.disabled = requestActive || chatState.blocked" not in script
    assert "hasMessage && !requestActive && !chatState.blocked" in script
    assert "if (!query.value.trim())" in script
    assert "restart.hidden = !chatState.blocked" in script
    assert 'restart.addEventListener("click", resetChatConversation)' in script
    assert "scrollChatToLatest" in script
    assert 'element("a", "answer-link", markdownLink[1])' in script
    assert 'link.rel = "noopener noreferrer"' in script
    assert "orderedList.start = Number(numbered[1])" in script
    assert "orderedList.append(currentOrderedItem)" in script
    assert "currentOrderedItem.append(nestedBulletList)" in script
    assert ".answer-link" in styles
    speaker_rule = re.search(r"\.chat-speaker\s*\{(?P<body>.*?)\}", styles, flags=re.DOTALL)
    assert speaker_rule is not None
    assert "text-transform" not in speaker_rule.group("body")
    assert "saved-project-card" in script
    assert "project-notes" in styles
    assert 'id="remove-saved-project-dialog"' in page
    assert 'aria-labelledby="remove-saved-project-title"' in page
    assert 'aria-describedby="remove-saved-project-description"' in page
    assert "Remove from My Projects?" in page
    assert "Any RepoScout notes saved with it will also be permanently removed." in page
    assert "searchable index will not be affected" in page
    assert page.index('id="remove-saved-project-cancel"') < page.index(
        'id="remove-saved-project-confirm"'
    )
    assert "data-remove-saved-project" in script
    assert "dialog.showModal()" in script
    assert script.index("dialog.showModal()") < script.index('method: "DELETE"')
    assert 'method: "DELETE"' in script
    assert "if (error instanceof ApiError && error.status === 404)" in script
    assert "This project was already removed from My Projects." in script
    assert "Do not retry" not in script
    assert "showSavedProjectsEmptyState(results)" in script
    assert ".remove-saved-project-button" in styles
    assert ".remove-saved-project-button {\n  border-color: var(--accent-border);" in styles
    assert ".confirmation-dialog" in styles
    assert ".confirmation-dialog::backdrop" in styles
    assert 'class="confirmation-button"' in page
    assert ".confirmation-button {" in styles
    assert "border: 1px solid var(--accent);" in styles
    assert "background: var(--accent);" in styles
    assert (
        ".dialog-status {\n  min-height: 24px;\n  margin-top: 16px;\n  color: var(--accent);"
        in styles
    )
    assert ".dialog-status.is-error {\n  color: var(--error-text);" in styles
    assert 'dialogStatus.classList.remove("is-error")' in script
    assert 'dialogStatus.classList.add("is-error")' in script
    assert "--destructive-contrast" not in styles
    assert 'dialog.addEventListener("cancel"' in script


def test_api_request_accepts_successful_empty_responses() -> None:
    script = (FRONTEND_ROOT / "assets" / "app.js").read_text()

    parse_start = script.index("async function apiRequest")
    parse_end = script.index("function parseSseEvent", parse_start)
    api_request = script[parse_start:parse_end]

    assert "let payload = null;" in api_request
    assert "payload = await response.json();" in api_request
    assert "catch" in api_request
    assert "if (!response.ok)" in api_request
    assert "return payload;" in api_request
