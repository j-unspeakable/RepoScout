import asyncio
import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest

from app.config import AppEnvironment, Settings
from app.dependencies import get_assistant_service
from app.main import create_app
from app.services.supervisor import (
    UNCERTAIN_COMPLETION_MESSAGE,
    AssistantCancellationOutcome,
    AssistantEvidenceChunk,
    AssistantEvidenceProject,
    AssistantPresentation,
    AssistantReply,
    AssistantService,
    ConversationConflictError,
    ConversationExpiredError,
    SupervisorBadGatewayError,
    SupervisorClient,
    SupervisorError,
    SupervisorProgressPhase,
    SupervisorResult,
    SupervisorTimeoutError,
    SupervisorTurnControl,
    SupervisorUnavailableError,
)


def _completed_response(answer: str = "Use owner/project.") -> dict[str, Any]:
    return {
        "id": "response-1",
        "status": "completed",
        "output": [
            {
                "type": "mcp_call",
                "id": "hidden-tool-call",
                "arguments": '{"repo_id":42}',
            },
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": answer}],
            },
        ],
    }


def _approval_response(
    identifier: str,
    tool_name: str = "search_projects",
) -> dict[str, Any]:
    return {
        "id": f"response-{identifier}",
        "status": "completed",
        "output": [
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "I'll use RepoScout."}],
            },
            {
                "type": "mcp_approval_request",
                "id": identifier,
                "name": tool_name,
                "server_label": "mcp-repo-scout",
                "arguments": '{"query":"data engineering"}',
            },
        ],
    }


def _sse_response(output: list[dict[str, Any]]) -> httpx.Response:
    events = [
        "event: response.output_item.done\n"
        f"data: {__import__('json').dumps({'type': 'response.output_item.done', 'item': item})}\n\n"
        for item in output
    ]
    return httpx.Response(
        200,
        headers={"Content-Type": "text/event-stream"},
        content="".join(events),
    )


def _project_payload(
    repo_id: int = 42,
    full_name: str = "owner/project",
    *,
    similarity: float | None = 0.92,
) -> dict[str, Any]:
    owner, name = full_name.split("/", maxsplit=1)
    return {
        "rank": 1,
        "repo_id": repo_id,
        "name": name,
        "full_name": full_name,
        "owner": owner,
        "description": "A safe repository description.",
        "html_url": f"https://github.com/{full_name}",
        "primary_language": "Python",
        "stars": 1200,
        "forks": 130,
        "open_issues": 12,
        "topics": ["data-engineering", "learning"],
        "license": "MIT",
        "similarity": similarity,
        "evidence": [
            {
                "chunk_id": f"hidden-{index}",
                "chunk_index": index,
                "chunk_text": f" README passage {index}. ",
                "similarity": 0.9,
            }
            for index in range(7)
        ],
    }


def _assistant_project(
    repo_id: int = 42,
    full_name: str = "owner/project",
) -> AssistantEvidenceProject:
    owner, name = full_name.split("/", maxsplit=1)
    return AssistantEvidenceProject(
        repo_id=repo_id,
        name=name,
        full_name=full_name,
        owner=owner,
        description="A safe repository description.",
        html_url=f"https://github.com/{full_name}",
        primary_language="Python",
        stars=1200,
        forks=130,
        open_issues=12,
        topics=("data-engineering", "learning"),
        license="MIT",
        similarity=0.92,
        evidence=(
            AssistantEvidenceChunk(
                chunk_index=0,
                chunk_text="README passage 0.",
                similarity=0.9,
            ),
        ),
    )


class FakeWorkspaceConfig:
    host = "https://workspace.example"

    def __init__(self) -> None:
        self.authentication_thread: int | None = None

    def authenticate(self) -> dict[str, str]:
        self.authentication_thread = threading.get_ident()
        return {"Authorization": "Bearer generated-token"}


class FakeWorkspace:
    def __init__(self) -> None:
        self.config = FakeWorkspaceConfig()


@pytest.fixture(autouse=True)
def supervisor_asyncify_bridge(monkeypatch: pytest.MonkeyPatch) -> list[object]:
    bridged_functions: list[object] = []

    def fake_asyncify(function: Any) -> Any:
        bridged_functions.append(function)

        async def call() -> Any:
            return function()

        return call

    monkeypatch.setattr("app.services.supervisor.asyncify", fake_asyncify)
    return bridged_functions


@pytest.mark.asyncio
async def test_supervisor_client_uses_local_profile_or_deployed_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DATABRICKS_CONFIG_PROFILE", raising=False)
    profiles: list[str | None] = []

    def workspace_factory(*, profile: str | None = None) -> FakeWorkspace:
        profiles.append(profile)
        return FakeWorkspace()

    monkeypatch.setattr("app.services.supervisor.WorkspaceClient", workspace_factory)
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json=_completed_response()))
    local_http = httpx.AsyncClient(transport=transport)
    deployed_http = httpx.AsyncClient(transport=transport)
    local = SupervisorClient(
        Settings(
            app_env=AppEnvironment.TEST,
            databricks_config_profile="reposcout",
            supervisor_endpoint_name="endpoint",
        ),
        client=local_http,
    )
    deployed = SupervisorClient(
        Settings(
            app_env=AppEnvironment.TEST,
            supervisor_endpoint_name="endpoint",
        ),
        client=deployed_http,
    )

    assert profiles == []
    await local.send([{"role": "user", "content": "find projects"}])
    await deployed.send([{"role": "user", "content": "find projects"}])

    assert profiles == ["reposcout", None]
    assert "endpoint" not in repr(local)
    assert "generated-token" not in repr(deployed)
    await local_http.aclose()
    await deployed_http.aclose()


@pytest.mark.asyncio
async def test_supervisor_client_uses_asyncify_and_parses_final_text(
    supervisor_asyncify_bridge: list[object],
) -> None:
    request_payload: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        request_payload.update(__import__("json").loads(request.content))
        assert request.url == "https://workspace.example/serving-endpoints/responses"
        assert request.headers["Authorization"] == "Bearer generated-token"
        return httpx.Response(200, json=_completed_response())

    workspace = FakeWorkspace()
    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = SupervisorClient(
        Settings(
            app_env=AppEnvironment.TEST,
            supervisor_endpoint_name="supervisor-endpoint",
        ),
        workspace_client=workspace,
        client=http_client,
    )
    result = await client.send([{"type": "message", "role": "user", "content": "Find RAG"}])

    assert result.answer == "Use owner/project."
    assert result.output_items[0]["type"] == "mcp_call"
    assert supervisor_asyncify_bridge == [client._request_context]
    assert request_payload == {
        "model": "supervisor-endpoint",
        "input": [{"type": "message", "role": "user", "content": "Find RAG"}],
        "stream": True,
        "databricks_options": {"long_task": True},
    }
    await http_client.aclose()


def test_supervisor_extracts_only_bounded_valid_repository_evidence() -> None:
    valid_project = {**_project_payload(), "secret": "must-not-leak"}
    output = [
        {
            "type": "mcp_call",
            "name": "search_projects",
            "arguments": '{"query":"private argument"}',
            "output": __import__("json").dumps(
                {
                    "query": "data engineering",
                    "projects": [
                        valid_project,
                        {
                            **valid_project,
                            "repo_id": 43,
                            "html_url": "https://example.com/owner/project",
                        },
                    ],
                }
            ),
        },
        {
            "type": "function_call_output",
            "output": __import__("json").dumps(
                {
                    **valid_project,
                    "evidence": [
                        {"chunk_index": 0, "chunk_text": "duplicate"},
                        {"chunk_index": 8, "chunk_text": "later passage"},
                    ],
                }
            ),
        },
    ]

    evidence = SupervisorClient._extract_evidence(output)

    assert evidence == (
        AssistantEvidenceProject(
            repo_id=42,
            name="project",
            full_name="owner/project",
            owner="owner",
            description="A safe repository description.",
            html_url="https://github.com/owner/project",
            primary_language="Python",
            stars=1200,
            forks=130,
            open_issues=12,
            topics=("data-engineering", "learning"),
            license="MIT",
            similarity=0.92,
            evidence=tuple(
                AssistantEvidenceChunk(
                    chunk_index=index,
                    chunk_text=f"README passage {index}.",
                    similarity=0.9,
                )
                for index in range(5)
            ),
        ),
    )
    serialized = repr(evidence)
    assert "secret" not in serialized
    assert "private argument" not in serialized
    assert "chunk_id" not in serialized


def test_supervisor_keeps_evidence_only_for_projects_named_in_answer() -> None:
    evidence = tuple(
        _assistant_project(index, full_name)
        for index, full_name in (
            (1, "owner/first-project"),
            (2, "owner/second-project"),
            (3, "owner/unmentioned-project"),
            (4, "owner/fastapi"),
        )
    )

    filtered = SupervisorClient._evidence_referenced_by_answer(
        (
            "Compare **owner/first-project** with "
            "[second-project](https://github.com/owner/second-project). "
            "Both are useful for FastAPI learners."
        ),
        evidence,
    )

    assert [project.repo_id for project in filtered] == [1, 2]


def test_supervisor_does_not_guess_ambiguous_history_project_names() -> None:
    evidence = (
        _assistant_project(1, "owner/project"),
        _assistant_project(2, "owner/project-extra"),
    )

    filtered = SupervisorClient._evidence_referenced_by_answer(
        "Tell me more about owner/project-extra.",
        evidence,
    )

    assert [project.repo_id for project in filtered] == [2]


def test_supervisor_selects_current_reads_history_references_and_suppresses_writes() -> None:
    search_output = [
        {
            "type": "mcp_call",
            "name": "search_projects",
            "output": __import__("json").dumps(
                {
                    "projects": [
                        _project_payload(1, "owner/first"),
                        _project_payload(2, "owner/second"),
                    ]
                }
            ),
        }
    ]
    current = SupervisorClient._select_visible_projects(
        "owner/second is the best fit.",
        [],
        search_output,
    )
    history = SupervisorClient._select_visible_projects(
        "Compare owner/first with owner/second.",
        search_output,
        [],
    )
    unresolved = SupervisorClient._select_visible_projects(
        "Tell me more about the second one.",
        search_output,
        [],
    )
    assert [project.repo_id for project in current] == [2]
    assert [project.repo_id for project in history] == [1, 2]
    assert unresolved == ()


def test_supervisor_limits_current_cards_to_unambiguously_recommended_projects() -> None:
    projects = [_project_payload(index, f"owner/repository-{index}") for index in range(1, 11)]
    current_items = [
        {
            "type": "mcp_call",
            "name": "search_projects",
            "output": __import__("json").dumps({"projects": projects}),
        }
    ]
    answer = "\n".join(
        [
            "1. **repository-1** is the clearest introduction.",
            "2. **repository-3** is more practical.",
            "3. **repository-5** covers production concerns.",
            "4. **repository-7** provides broader examples.",
            "5. **repository-9** is the best structured starting point.",
        ]
    )

    visible = SupervisorClient._select_visible_projects(answer, [], current_items)

    assert [project.repo_id for project in visible] == [1, 3, 5, 7, 9]


def test_supervisor_omits_ambiguous_current_repository_name_references() -> None:
    current_items = [
        {
            "type": "mcp_call",
            "name": "search_projects",
            "output": __import__("json").dumps(
                {
                    "projects": [
                        _project_payload(1, "first-owner/project"),
                        _project_payload(2, "second-owner/project"),
                    ]
                }
            ),
        }
    ]

    ambiguous = SupervisorClient._select_visible_projects(
        "project is the better starting point.",
        [],
        current_items,
    )
    exact = SupervisorClient._select_visible_projects(
        "first-owner/project is the better starting point.",
        [],
        current_items,
    )

    assert ambiguous == ()
    assert [project.repo_id for project in exact] == [1]


def test_supervisor_retains_current_projects_when_answer_names_none() -> None:
    current_items = [
        {
            "type": "mcp_call",
            "name": "search_projects",
            "output": __import__("json").dumps(
                {
                    "projects": [
                        _project_payload(1, "owner/first"),
                        _project_payload(2, "owner/second"),
                    ]
                }
            ),
        }
    ]

    visible = SupervisorClient._select_visible_projects(
        "These options support different learning styles.",
        [],
        current_items,
    )

    assert [project.repo_id for project in visible] == [1, 2]


def test_supervisor_deduplicates_current_projects_in_tool_result_order() -> None:
    first = _project_payload(1, "owner/first", similarity=0.81)
    duplicate = _project_payload(1, "owner/first", similarity=None)
    duplicate["description"] = "Later details must not replace the ranked search result."
    output = [
        {
            "type": "mcp_call",
            "name": "search_projects",
            "output": __import__("json").dumps(
                {"projects": [first, _project_payload(2, "owner/second")]}
            ),
        },
        {
            "type": "mcp_call",
            "name": "get_project_details",
            "output": __import__("json").dumps(duplicate),
        },
    ]

    projects = SupervisorClient._extract_evidence(output)

    assert [project.repo_id for project in projects] == [1, 2]
    assert projects[0].similarity == 0.81
    assert projects[0].description == "A safe repository description."


def test_supervisor_prioritizes_current_project_details_over_search_candidates() -> None:
    output = [
        {
            "type": "mcp_call",
            "name": "search_projects",
            "output": __import__("json").dumps(
                {
                    "projects": [
                        _project_payload(1, "owner/first"),
                        _project_payload(2, "owner/second"),
                        _project_payload(3, "owner/third"),
                    ]
                }
            ),
        },
        {
            "type": "mcp_call",
            "name": "get_project_details",
            "output": __import__("json").dumps(_project_payload(1, "owner/first", similarity=None)),
        },
    ]

    visible = SupervisorClient._select_visible_projects(
        "It offers a practical, guided learning path.",
        [],
        output,
    )

    assert [project.repo_id for project in visible] == [1]
    assert visible[0].similarity is None


@pytest.mark.parametrize(
    ("message", "tool_names", "has_evidence", "expected"),
    [
        ("Save it", {"save_project"}, True, AssistantPresentation.TEXT),
        ("Save it", set(), True, AssistantPresentation.TEXT),
        ("Add a note about pipeline design", set(), True, AssistantPresentation.TEXT),
        (
            "Mark DataTalksClub/data-engineering-zoomcamp as To Try.",
            set(),
            True,
            AssistantPresentation.TEXT,
        ),
        ("Find projects", {"search_projects"}, False, AssistantPresentation.TEXT),
        (
            "Compare these projects and show the README evidence",
            {"get_project_details"},
            True,
            AssistantPresentation.CARDS,
        ),
        (
            "Compare the two strongest options",
            {"search_projects"},
            True,
            AssistantPresentation.REFERENCES,
        ),
        (
            "Tell me more about it",
            {"get_project_details"},
            True,
            AssistantPresentation.REFERENCES,
        ),
        (
            "Recommend three projects",
            {"search_projects"},
            True,
            AssistantPresentation.CARDS,
        ),
        (
            "What should I consider next?",
            set(),
            True,
            AssistantPresentation.REFERENCES,
        ),
    ],
)
def test_supervisor_selects_intent_aware_presentation_with_fixed_precedence(
    message: str,
    tool_names: set[str],
    has_evidence: bool,
    expected: AssistantPresentation,
) -> None:
    current_items = [{"type": "mcp_call", "name": name, "output": "{}"} for name in tool_names]
    evidence = (_assistant_project(),) if has_evidence else ()

    assert SupervisorClient._select_presentation(message, evidence, current_items) is expected


@pytest.mark.parametrize(
    ("answer", "evidence", "expected"),
    [
        (
            "See https://github.com/owner/project for the full repository.",
            (_assistant_project(42, "owner/project"),),
            "See owner/project for the full repository.",
        ),
        (
            "The result has repo_id: 42 and is worth exploring.",
            (_assistant_project(42, "owner/project"),),
            "The result is worth exploring.",
        ),
        (
            "1. owner/first — 1,200 stars and 130 forks\n2. owner/second — 900 stars and 80 forks",
            (_assistant_project(1, "owner/first"), _assistant_project(2, "owner/second")),
            SupervisorClient._MULTIPLE_PROJECT_FALLBACK,
        ),
    ],
)
def test_supervisor_sanitizes_or_replaces_duplicative_project_prose(
    answer: str,
    evidence: tuple[AssistantEvidenceProject, ...],
    expected: str,
) -> None:
    assert SupervisorClient._normalize_visible_answer(answer, evidence) == expected


def test_supervisor_uses_combined_supporting_signals_but_not_length_alone() -> None:
    evidence = (
        _assistant_project(1, "owner/first"),
        _assistant_project(2, "owner/second"),
    )
    useful_comparison = (
        "owner/first is more structured, while owner/second offers broader coverage. "
        + "The former provides a guided path; the latter rewards exploratory learning. " * 45
    )
    repeated_metadata = (
        "owner/first has 1,200 stars, while owner/second has 900 stars. "
        + "The first is more structured. " * 60
    )

    assert len(useful_comparison) > SupervisorClient._PROSE_CHARACTER_GUIDELINE
    assert len(useful_comparison.split()) > SupervisorClient._PROSE_WORD_GUIDELINE
    assert (
        SupervisorClient._normalize_visible_answer(useful_comparison, evidence)
        == useful_comparison.strip()
    )
    assert (
        SupervisorClient._normalize_visible_answer(repeated_metadata, evidence)
        == SupervisorClient._MULTIPLE_PROJECT_FALLBACK
    )


def test_supervisor_replaces_live_style_name_only_catalogue() -> None:
    evidence = (
        _assistant_project(1, "owner/ultimate-fastapi-tutorial"),
        _assistant_project(2, "owner/FastAPI-The-Complete-Course"),
        _assistant_project(3, "owner/FastAPI-Learning-Example"),
    )
    answer = (
        "Based on the search results, here are three projects:\n\n"
        "- **ultimate-fastapi-tutorial** offers structured learning and detailed explanations.\n"
        "- **FastAPI-The-Complete-Course** provides systematic course coverage.\n"
        "- **FastAPI-Learning-Example** takes a practical, use-case-driven approach.\n\n"
        + "The tutorials cover different learning styles. "
        * 35
    )

    assert (
        SupervisorClient._normalize_visible_answer(answer, evidence)
        == SupervisorClient._MULTIPLE_PROJECT_FALLBACK
    )


def test_supervisor_replaces_compact_paragraph_catalogue_with_repeated_metadata() -> None:
    evidence = (
        _assistant_project(1, "owner/first"),
        _assistant_project(2, "owner/second"),
        _assistant_project(3, "owner/third"),
        _assistant_project(4, "owner/fourth"),
    )
    answer = (
        "I found the top five resources, although the search returned four.\n\n"
        "owner/first leads with 44,425 stars.\n\n"
        "owner/second follows with 43,472 stars.\n\n"
        "owner/third has 29,782 stars.\n\n"
        "owner/fourth has 14,139 stars."
    )

    assert len(answer) < SupervisorClient._PROSE_CHARACTER_GUIDELINE
    assert len(answer.split()) < SupervisorClient._PROSE_WORD_GUIDELINE
    assert (
        SupervisorClient._normalize_visible_answer(answer, evidence)
        == SupervisorClient._MULTIPLE_PROJECT_FALLBACK
    )


def test_supervisor_keeps_single_project_explanation_instead_of_generic_fallback() -> None:
    evidence = (_assistant_project(1, "owner/ultimate-fastapi-tutorial"),)
    answer = (
        "**owner/ultimate-fastapi-tutorial** uses a dual-format learning structure.\n\n"
        "**Core Structure:**\n\n"
        "1. Blog post series with detailed explanations.\n"
        "2. Companion code corresponding to the posts.\n\n"
        + "The material follows a sequential learning path. " * 35
        + "It has 377 forks."
    )

    assert SupervisorClient._normalize_visible_answer(answer, evidence) == answer


@pytest.mark.parametrize(
    "answer",
    [
        (
            "README Evidence Retrieved\n\n"
            "Chunk 1 (similarity: 0.666): practical pipeline course material."
        ),
        "Chunk 1: practical pipeline course material.\nChunk 2: a structured final project.",
        (
            "1. Course overview (Chunk 0)\n2. Detailed syllabus (Chunks 3-4)\n"
            "The query achieved a 0.753 similarity score."
        ),
    ],
)
def test_supervisor_replaces_raw_single_project_evidence_catalogue(answer: str) -> None:
    assert (
        SupervisorClient._normalize_visible_answer(
            answer,
            (_assistant_project(1, "owner/project"),),
            AssistantPresentation.CARDS,
        )
        == SupervisorClient._SINGLE_PROJECT_FALLBACK
    )


def test_supervisor_reference_mode_keeps_reasoning_and_only_performs_safe_cleanup() -> None:
    evidence = (_assistant_project(42, "owner/project"),)
    answer = (
        "[owner/project](https://github.com/owner/project) (repo_id: 42) is the stronger "
        "choice because its guided examples build toward a complete pipeline."
    )
    unsafe_inline_id = "The internal repo_id: 42 identifies the project in this sentence."
    unknown_link = "Compare it with https://github.com/another/project before deciding."
    empty_list_item = "- (repo_id: 42)"

    assert SupervisorClient._normalize_visible_answer(
        answer,
        evidence,
        AssistantPresentation.REFERENCES,
    ) == (
        "owner/project is the stronger choice because its guided examples build toward a "
        "complete pipeline."
    )
    assert (
        SupervisorClient._normalize_visible_answer(
            unsafe_inline_id,
            evidence,
            AssistantPresentation.REFERENCES,
        )
        == unsafe_inline_id
    )
    assert (
        SupervisorClient._normalize_visible_answer(
            unknown_link,
            evidence,
            AssistantPresentation.REFERENCES,
        )
        == unknown_link
    )
    assert (
        SupervisorClient._normalize_visible_answer(
            empty_list_item,
            evidence,
            AssistantPresentation.REFERENCES,
        )
        == empty_list_item
    )


def test_supervisor_catalogue_fallback_is_limited_to_card_mode() -> None:
    evidence = (
        _assistant_project(1, "owner/first"),
        _assistant_project(2, "owner/second"),
    )
    answer = "1. owner/first — 1,200 stars and 130 forks\n2. owner/second — 900 stars and 80 forks"

    assert (
        SupervisorClient._normalize_visible_answer(
            answer,
            evidence,
            AssistantPresentation.CARDS,
        )
        == SupervisorClient._MULTIPLE_PROJECT_FALLBACK
    )
    assert (
        SupervisorClient._normalize_visible_answer(
            answer,
            evidence,
            AssistantPresentation.REFERENCES,
        )
        == answer
    )


def test_supervisor_card_fallbacks_explain_available_follow_up_actions() -> None:
    single = SupervisorClient._SINGLE_PROJECT_FALLBACK
    multiple = SupervisorClient._MULTIPLE_PROJECT_FALLBACK

    assert all(action in single for action in ("explain", "save", "status", "note"))
    assert all(action in multiple for action in ("compare", "explain", "save", "status", "notes"))


def test_supervisor_keeps_concise_interpretation_and_text_only_turns() -> None:
    interpretation = (
        "owner/first is the more structured starting point, while owner/second is useful "
        "when you want broader examples."
    )

    assert (
        SupervisorClient._normalize_visible_answer(
            interpretation,
            (_assistant_project(1, "owner/first"), _assistant_project(2, "owner/second")),
        )
        == interpretation
    )
    assert (
        SupervisorClient._normalize_visible_answer(
            "Saved owner/first.",
            (),
        )
        == "Saved owner/first."
    )


@pytest.mark.asyncio
async def test_supervisor_returns_normalized_text_but_retains_original_output_for_replay() -> None:
    original_answer = "1. [project](https://github.com/owner/project) — 1,200 stars and 130 forks"
    response = {
        "status": "completed",
        "output": [
            {
                "type": "mcp_call",
                "name": "search_projects",
                "output": __import__("json").dumps({"projects": [_project_payload()]}),
            },
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": original_answer}],
            },
        ],
    }
    http_client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=response))
    )
    client = SupervisorClient(
        Settings(app_env=AppEnvironment.TEST, supervisor_endpoint_name="endpoint"),
        workspace_client=FakeWorkspace(),
        client=http_client,
    )

    result = await client.send([{"type": "message", "role": "user", "content": "Find a project"}])

    assert result.answer == "1. project — 1,200 stars and 130 forks"
    assert result.evidence[0].repo_id == 42
    assert result.presentation is AssistantPresentation.CARDS
    assert result.output_items[-1]["content"][0]["text"] == original_answer
    await http_client.aclose()


@pytest.mark.parametrize(
    "tool_name",
    ["save_project", "update_project_status", "add_project_note"],
)
def test_supervisor_write_actions_suppress_retained_project_cards(tool_name: str) -> None:
    history = [
        {
            "type": "mcp_call",
            "name": "search_projects",
            "output": __import__("json").dumps({"projects": [_project_payload(1, "owner/first")]}),
        }
    ]
    current = [
        {
            "type": "mcp_approval_request",
            "id": "write-1",
            "name": tool_name,
        }
    ]

    visible = SupervisorClient._select_visible_projects(
        "The action completed for owner/first.",
        history,
        current,
    )

    assert visible == ()


def test_supervisor_extracts_project_details_without_inventing_similarity() -> None:
    details = _project_payload(similarity=None)
    for chunk in details["evidence"]:
        chunk.pop("similarity")
    output = [
        {
            "type": "mcp_approval_request",
            "id": "details-1",
            "name": "get_project_details",
        },
        {
            "type": "function_call_output",
            "call_id": "details-1",
            "output": __import__("json").dumps(details),
        },
    ]

    projects = SupervisorClient._extract_evidence(output)

    assert len(projects) == 1
    assert projects[0].similarity is None
    assert all(chunk.similarity is None for chunk in projects[0].evidence)


@pytest.mark.asyncio
async def test_supervisor_client_approves_mcp_and_returns_only_final_answer() -> None:
    requests: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = __import__("json").loads(request.content)
        requests.append(payload)
        if len(requests) == 1:
            approval_payload = _approval_response("approval-1")
            approval_payload["output"].append(
                {
                    "type": "mcp_approval_request",
                    "id": "approval-2",
                    "name": "get_project_details",
                    "server_label": "mcp-repo-scout",
                    "arguments": '{"repo_id":42}',
                }
            )
            return httpx.Response(200, json=approval_payload)
        return httpx.Response(
            200,
            json={
                "status": "completed",
                "output": [
                    {
                        "type": "function_call_output",
                        "call_id": "approval-1",
                        "output": '{"projects":[]}',
                    },
                    {
                        "type": "function_call_output",
                        "call_id": "approval-2",
                        "output": '{"repo_id":42}',
                    },
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "No matches."}],
                    },
                ],
            },
        )

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = SupervisorClient(
        Settings(app_env=AppEnvironment.TEST, supervisor_endpoint_name="endpoint"),
        workspace_client=FakeWorkspace(),
        client=http_client,
    )
    original_input = [
        {"type": "message", "role": "user", "content": "Find data engineering projects"}
    ]

    result = await client.send(original_input)

    assert result.answer == "No matches."
    assert len(requests) == 2
    assert requests[0]["input"] == original_input
    assert [item["type"] for item in requests[1]["input"][-5:]] == [
        "message",
        "mcp_approval_request",
        "mcp_approval_response",
        "mcp_approval_request",
        "mcp_approval_response",
    ]
    assert requests[1]["input"][-4]["id"] == "approval-1"
    assert requests[1]["input"][-3]["approval_request_id"] == "approval-1"
    assert requests[1]["input"][-2]["id"] == "approval-2"
    assert requests[1]["input"][-1]["approval_request_id"] == "approval-2"
    assert [item["type"] for item in result.output_items] == [
        "message",
        "mcp_approval_request",
        "mcp_approval_response",
        "mcp_approval_request",
        "mcp_approval_response",
        "function_call_output",
        "function_call_output",
        "message",
    ]
    assert "I'll use RepoScout." not in result.answer
    await http_client.aclose()


@pytest.mark.asyncio
async def test_supervisor_streams_and_replays_task_continuation_through_input() -> None:
    requests: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = __import__("json").loads(request.content)
        requests.append(payload)
        if len(requests) == 1:
            return _sse_response(
                [{"type": "task_continue_request", "id": "continue-1", "step": 16}]
            )
        return _sse_response(_completed_response("Finished.")["output"])

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = SupervisorClient(
        Settings(app_env=AppEnvironment.TEST, supervisor_endpoint_name="endpoint"),
        workspace_client=FakeWorkspace(),
        client=http_client,
    )
    phases: list[SupervisorProgressPhase] = []

    async def progress(phase: SupervisorProgressPhase) -> None:
        phases.append(phase)

    result = await client.send(
        [{"type": "message", "role": "user", "content": "continue"}],
        progress=progress,
    )

    assert result.answer == "Finished."
    assert requests[0]["stream"] is True
    assert requests[0]["databricks_options"] == {"long_task": True}
    assert "messages" not in requests[0]
    assert requests[1]["input"][-2:] == [
        {"type": "task_continue_request", "id": "continue-1", "step": 16},
        {"type": "task_continue_response", "continue_request_id": "continue-1"},
    ]
    assert phases == [
        SupervisorProgressPhase.WORKING,
        SupervisorProgressPhase.CONTINUING,
        SupervisorProgressPhase.FINISHING,
    ]
    await http_client.aclose()


@pytest.mark.asyncio
async def test_supervisor_coalesces_repeated_tool_progress() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return _sse_response(_approval_response(f"search-{calls}", "search_projects")["output"])
        if calls == 2:
            return _sse_response(
                [
                    {"type": "function_call_output", "call_id": "search-1", "output": "{}"},
                    *_approval_response("search-2", "search_projects")["output"],
                ]
            )
        return _sse_response(
            [
                {"type": "function_call_output", "call_id": "search-2", "output": "{}"},
                *_completed_response("Done.")["output"],
            ]
        )

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = SupervisorClient(
        Settings(app_env=AppEnvironment.TEST, supervisor_endpoint_name="endpoint"),
        workspace_client=FakeWorkspace(),
        client=http_client,
    )
    phases: list[SupervisorProgressPhase] = []

    async def progress(phase: SupervisorProgressPhase) -> None:
        phases.append(phase)

    await client.send([{"role": "user", "content": "find"}], progress=progress)

    assert phases.count(SupervisorProgressPhase.SEARCHING_PROJECTS) == 1
    assert phases[-1] is SupervisorProgressPhase.FINISHING
    await http_client.aclose()


@pytest.mark.asyncio
async def test_supervisor_timeout_after_write_dispatch_is_uncertain() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return _sse_response(_approval_response("save-1", "save_project")["output"])
        raise httpx.ReadTimeout("private", request=request)

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = SupervisorClient(
        Settings(app_env=AppEnvironment.TEST, supervisor_endpoint_name="endpoint"),
        workspace_client=FakeWorkspace(),
        client=http_client,
    )

    with pytest.raises(SupervisorTimeoutError) as caught:
        await client.send([{"role": "user", "content": "save it"}])

    assert caught.value.uncertain is True
    assert str(caught.value) == UNCERTAIN_COMPLETION_MESSAGE
    await http_client.aclose()


@pytest.mark.asyncio
async def test_supervisor_rejects_non_monotonic_continuation_steps() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return _sse_response(
            [
                {
                    "type": "task_continue_request",
                    "id": f"continue-{calls}",
                    "step": 16,
                }
            ]
        )

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = SupervisorClient(
        Settings(app_env=AppEnvironment.TEST, supervisor_endpoint_name="endpoint"),
        workspace_client=FakeWorkspace(),
        client=http_client,
    )

    with pytest.raises(SupervisorBadGatewayError) as caught:
        await client.send([{"role": "user", "content": "continue"}])

    assert caught.value.uncertain is False
    assert calls == 2
    await http_client.aclose()


@pytest.mark.parametrize(
    "tool_name",
    [
        "search_projects",
        "get_project_details",
        "save_project",
        "update_project_status",
        "add_project_note",
    ],
)
def test_supervisor_client_approval_allowlist(tool_name: str) -> None:
    response = SupervisorClient._approval_responses(
        _approval_response("approval-1", tool_name)["output"]
    )

    assert response == [
        {
            "type": "mcp_approval_response",
            "id": "approval-1",
            "approval_request_id": "approval-1",
            "approve": True,
        }
    ]


@pytest.mark.asyncio
async def test_supervisor_client_rejects_unknown_or_malformed_mcp_approvals() -> None:
    payloads = [
        _approval_response("approval-1", "delete_project"),
        _approval_response("", "search_projects"),
        {
            "status": "completed",
            "output": [
                *_approval_response("duplicate", "search_projects")["output"],
                {
                    "type": "mcp_approval_request",
                    "id": "duplicate",
                    "name": "get_project_details",
                },
            ],
        },
    ]

    for payload in payloads:
        calls = 0

        def handler(
            request: httpx.Request, response_payload: dict[str, Any] = payload
        ) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(200, json=response_payload)

        http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        client = SupervisorClient(
            Settings(app_env=AppEnvironment.TEST, supervisor_endpoint_name="endpoint"),
            workspace_client=FakeWorkspace(),
            client=http_client,
        )

        with pytest.raises(SupervisorBadGatewayError) as caught:
            await client.send([{"role": "user", "content": "find projects"}])

        assert caught.value.uncertain is False
        assert calls == 1
        await http_client.aclose()


@pytest.mark.asyncio
async def test_supervisor_client_bounds_mcp_approval_rounds() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json=_approval_response(f"approval-{calls}"))

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = SupervisorClient(
        Settings(app_env=AppEnvironment.TEST, supervisor_endpoint_name="endpoint"),
        workspace_client=FakeWorkspace(),
        client=http_client,
    )
    client._MAX_MCP_APPROVAL_ROUNDS = 2

    with pytest.raises(SupervisorBadGatewayError) as caught:
        await client.send([{"role": "user", "content": "find projects"}])

    assert caught.value.uncertain is False
    assert calls == 3
    await http_client.aclose()


@pytest.mark.asyncio
async def test_supervisor_client_rejects_message_without_result_after_approval() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(200, json=_approval_response("approval-1"))
        return httpx.Response(
            200,
            json={
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [
                            {
                                "type": "output_text",
                                "text": "Invalid approval response. Internal detail.",
                            }
                        ],
                    }
                ],
            },
        )

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = SupervisorClient(
        Settings(app_env=AppEnvironment.TEST, supervisor_endpoint_name="endpoint"),
        workspace_client=FakeWorkspace(),
        client=http_client,
    )

    with pytest.raises(SupervisorBadGatewayError) as caught:
        await client.send([{"role": "user", "content": "find projects"}])

    assert caught.value.uncertain is False
    assert "Internal detail" not in str(caught.value)
    assert calls == 2
    await http_client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status_code", "error_type"),
    [
        (429, SupervisorUnavailableError),
        (503, SupervisorUnavailableError),
        (504, SupervisorTimeoutError),
        (400, SupervisorBadGatewayError),
    ],
)
async def test_supervisor_client_maps_safe_http_errors_before_write_dispatch(
    status_code: int, error_type: type[SupervisorError]
) -> None:
    client = SupervisorClient(
        Settings(app_env=AppEnvironment.TEST, supervisor_endpoint_name="endpoint"),
        workspace_client=FakeWorkspace(),
        client=httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    status_code,
                    headers={"Retry-After": "9"},
                    json={"error": {"message": "raw provider detail"}},
                )
            )
        ),
    )

    with pytest.raises(error_type) as caught:
        await client.send([{"role": "user", "content": "save it"}])

    assert caught.value.uncertain is False
    assert "raw provider detail" not in str(caught.value)
    if isinstance(caught.value, SupervisorUnavailableError):
        assert caught.value.retry_after == 9


@pytest.mark.asyncio
async def test_supervisor_client_rejects_incomplete_or_empty_responses() -> None:
    payloads = (
        {"status": "incomplete", "output": []},
        {"status": "completed", "output": []},
        {"status": "completed", "output": [{"type": "message", "content": []}]},
    )
    for payload in payloads:
        http_client = httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request, response_payload=payload: httpx.Response(200, json=response_payload)
            )
        )
        client = SupervisorClient(
            Settings(app_env=AppEnvironment.TEST, supervisor_endpoint_name="endpoint"),
            workspace_client=FakeWorkspace(),
            client=http_client,
        )
        with pytest.raises(SupervisorBadGatewayError) as caught:
            await client.send([{"role": "user", "content": "find projects"}])
        assert caught.value.uncertain is False
        await http_client.aclose()


@pytest.mark.asyncio
async def test_supervisor_rejects_explicit_incomplete_stream_event() -> None:
    http_client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                headers={"Content-Type": "text/event-stream"},
                content=('event: response.incomplete\ndata: {"type":"response.incomplete"}\n\n'),
            )
        )
    )
    client = SupervisorClient(
        Settings(app_env=AppEnvironment.TEST, supervisor_endpoint_name="endpoint"),
        workspace_client=FakeWorkspace(),
        client=http_client,
    )

    with pytest.raises(SupervisorBadGatewayError) as caught:
        await client.send([{"role": "user", "content": "find projects"}])

    assert caught.value.uncertain is False
    await http_client.aclose()


@pytest.mark.asyncio
async def test_supervisor_client_timeout_before_write_dispatch_is_safe() -> None:
    def timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("raw timeout detail", request=request)

    client = SupervisorClient(
        Settings(app_env=AppEnvironment.TEST, supervisor_endpoint_name="endpoint"),
        workspace_client=FakeWorkspace(),
        client=httpx.AsyncClient(transport=httpx.MockTransport(timeout)),
    )

    with pytest.raises(SupervisorTimeoutError) as caught:
        await client.send([{"role": "user", "content": "add a note"}])

    assert str(caught.value) == "Ask RepoScout timed out. Please try again."
    assert caught.value.uncertain is False
    assert "raw timeout detail" not in str(caught.value)


class RecordingSupervisorClient:
    def __init__(self) -> None:
        self.inputs: list[list[dict[str, Any]]] = []
        self.error: Exception | None = None

    async def send(
        self,
        input_items: list[dict[str, Any]],
        *,
        progress: Any = None,
        control: Any = None,
    ) -> SupervisorResult:
        self.inputs.append(input_items)
        if self.error:
            raise self.error
        answer = f"Answer {len(self.inputs)}"
        return SupervisorResult(answer=answer, output_items=_completed_response(answer)["output"])


@pytest.mark.asyncio
async def test_assistant_service_replays_hidden_output_and_bounds_complete_turns() -> None:
    client = RecordingSupervisorClient()
    service = AssistantService(client, max_turns=2)

    first = await service.send(None, "Find projects")
    await service.send(first.conversation_id, "Tell me about the second one")
    await service.send(first.conversation_id, "Save it")
    await service.send(first.conversation_id, "What did I save?")

    final_input = client.inputs[-1]
    assert [item.get("content") for item in final_input if item.get("role") == "user"] == [
        "Tell me about the second one",
        "Save it",
        "What did I save?",
    ]
    assert any(item.get("id") == "hidden-tool-call" for item in final_input)


@pytest.mark.asyncio
async def test_failed_turn_is_not_committed_to_conversation_history() -> None:
    client = RecordingSupervisorClient()
    service = AssistantService(client)
    first = await service.send(None, "Find projects")
    client.error = SupervisorTimeoutError(UNCERTAIN_COMPLETION_MESSAGE, uncertain=True)
    with pytest.raises(SupervisorTimeoutError):
        await service.send(first.conversation_id, "Add a note")

    client.error = None
    await service.send(first.conversation_id, "What did you find?")

    latest_user_messages = [
        item.get("content") for item in client.inputs[-1] if item.get("role") == "user"
    ]
    assert latest_user_messages == ["Find projects", "What did you find?"]


@pytest.mark.asyncio
async def test_conversation_expires_after_inactivity() -> None:
    now = [0.0]
    service = AssistantService(RecordingSupervisorClient(), ttl_seconds=60, clock=lambda: now[0])
    first = await service.send(None, "Find projects")
    now[0] = 61.0

    with pytest.raises(ConversationExpiredError):
        await service.send(first.conversation_id, "Save it")


class BlockingSupervisorClient:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def send(
        self,
        input_items: list[dict[str, Any]],
        *,
        progress: Any = None,
        control: Any = None,
    ) -> SupervisorResult:
        self.started.set()
        await self.release.wait()
        return SupervisorResult(answer="Done", output_items=_completed_response("Done")["output"])


@pytest.mark.asyncio
async def test_concurrent_turn_is_rejected() -> None:
    client = BlockingSupervisorClient()
    identifier = uuid4()
    service = AssistantService(client, id_factory=lambda: identifier)
    task = asyncio.create_task(service.send(None, "Find projects"))
    await client.started.wait()

    with pytest.raises(ConversationConflictError):
        await service.send(identifier, "Save it")

    client.release.set()
    await task


class CancellableSupervisorClient:
    def __init__(self) -> None:
        self.calls = 0
        self.started = asyncio.Event()

    async def send(
        self,
        input_items: list[dict[str, Any]],
        *,
        progress: Any = None,
        control: Any = None,
    ) -> SupervisorResult:
        self.calls += 1
        if self.calls == 2:
            self.started.set()
            await asyncio.Event().wait()
        return SupervisorResult(answer="Done", output_items=_completed_response("Done")["output"])


@pytest.mark.asyncio
async def test_cancelled_turn_releases_conversation_without_committing() -> None:
    client = CancellableSupervisorClient()
    service = AssistantService(client)
    first = await service.send(None, "Find projects")
    cancelled = asyncio.create_task(service.send(first.conversation_id, "Add a note"))
    await client.started.wait()

    cancelled.cancel()
    with pytest.raises(asyncio.CancelledError):
        await cancelled

    reply = await service.send(first.conversation_id, "What did you find?")
    assert reply.content == "Done"


class ControlledTurnSupervisorClient:
    def __init__(self, *, write: bool = False) -> None:
        self.calls = 0
        self.write = write
        self.started = asyncio.Event()

    async def send(
        self,
        input_items: list[dict[str, Any]],
        *,
        progress: Any = None,
        control: SupervisorTurnControl | None = None,
    ) -> SupervisorResult:
        self.calls += 1
        if self.calls != 2:
            return SupervisorResult(
                answer="Done",
                output_items=_completed_response("Done")["output"],
            )
        assert control is not None
        if self.write:
            await control.prepare_followup(["save_project"], frozenset({"save_project"}))
            if progress:
                await progress(SupervisorProgressPhase.SAVING_PROJECTS)
        elif progress:
            await progress(SupervisorProgressPhase.SEARCHING_PROJECTS)
        self.started.set()
        await asyncio.Event().wait()
        raise AssertionError("cancelled turn continued")


async def _ignore_progress(phase: SupervisorProgressPhase) -> None:
    del phase


@pytest.mark.asyncio
async def test_stream_turn_safe_cancellation_after_read_only_activity_keeps_chat_usable() -> None:
    client = ControlledTurnSupervisorClient()
    service = AssistantService(client)
    first = await service.send(None, "find projects")
    turn_id = uuid4()
    task = await service.start_turn(
        turn_id,
        first.conversation_id,
        "review details",
        progress=_ignore_progress,
    )
    await client.started.wait()

    cancellation = await service.cancel_turn(turn_id)

    assert cancellation.outcome is AssistantCancellationOutcome.CANCELLED
    assert task.cancelled()
    reply = await service.send(first.conversation_id, "continue")
    assert reply.content == "Done"


@pytest.mark.asyncio
async def test_stream_turn_cancellation_after_write_dispatch_is_uncertain() -> None:
    client = ControlledTurnSupervisorClient(write=True)
    service = AssistantService(client)
    first = await service.send(None, "find projects")
    turn_id = uuid4()
    await service.start_turn(
        turn_id,
        first.conversation_id,
        "save it",
        progress=_ignore_progress,
    )
    await client.started.wait()

    cancellation = await service.cancel_turn(turn_id)

    assert cancellation.outcome is AssistantCancellationOutcome.UNCERTAIN


@pytest.mark.asyncio
async def test_completed_turn_tombstone_resolves_stop_race_then_expires() -> None:
    now = [0.0]
    service = AssistantService(
        RecordingSupervisorClient(),
        completed_turn_ttl_seconds=60,
        clock=lambda: now[0],
    )
    turn_id = uuid4()
    task = await service.start_turn(turn_id, None, "find", progress=_ignore_progress)
    reply = await task

    completed = await service.cancel_turn(turn_id)
    now[0] = 61.0
    expired = await service.cancel_turn(turn_id)

    assert completed.outcome is AssistantCancellationOutcome.COMPLETED
    assert completed.reply == reply
    assert expired.outcome is AssistantCancellationOutcome.UNCERTAIN


@pytest.mark.asyncio
async def test_completed_turn_tombstones_are_capacity_bounded() -> None:
    service = AssistantService(RecordingSupervisorClient(), max_completed_turns=1)
    first_id = uuid4()
    second_id = uuid4()
    await (await service.start_turn(first_id, None, "first", progress=_ignore_progress))
    await (await service.start_turn(second_id, None, "second", progress=_ignore_progress))

    assert (await service.cancel_turn(first_id)).outcome is AssistantCancellationOutcome.UNCERTAIN
    assert (await service.cancel_turn(second_id)).outcome is AssistantCancellationOutcome.COMPLETED


class FakeAssistantService:
    def __init__(self) -> None:
        self.error: Exception | None = None
        self.evidence: tuple[AssistantEvidenceProject, ...] = ()

    async def send(self, conversation_id: UUID | None, message: str) -> AssistantReply:
        if self.error:
            raise self.error
        return AssistantReply(
            conversation_id=conversation_id or uuid4(),
            content="Safe answer",
            evidence=self.evidence,
            presentation=(
                AssistantPresentation.CARDS if self.evidence else AssistantPresentation.TEXT
            ),
        )


@asynccontextmanager
async def _assistant_api_client(
    service: Any | None = None,
) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(Settings(app_env=AppEnvironment.TEST))
    if service is not None:

        async def override() -> FakeAssistantService:
            return service

        app.dependency_overrides[get_assistant_service] = override
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            yield client


@pytest.mark.asyncio
async def test_assistant_endpoint_returns_only_visible_message_contract() -> None:
    service = FakeAssistantService()
    service.evidence = (_assistant_project(),)
    async with _assistant_api_client(service) as client:
        response = await client.post("/assistant/messages", json={"message": "  Find projects  "})

    assert response.status_code == 200
    assert set(response.json()) == {"conversation_id", "message"}
    assert response.json()["message"] == {
        "role": "assistant",
        "content": "Safe answer",
        "presentation": "cards",
        "evidence": [
            {
                "repo_id": 42,
                "name": "project",
                "full_name": "owner/project",
                "owner": "owner",
                "description": "A safe repository description.",
                "html_url": "https://github.com/owner/project",
                "primary_language": "Python",
                "stars": 1200,
                "forks": 130,
                "open_issues": 12,
                "topics": ["data-engineering", "learning"],
                "license": "MIT",
                "similarity": 0.92,
                "evidence": [
                    {
                        "chunk_index": 0,
                        "chunk_text": "README passage 0.",
                        "similarity": 0.9,
                    },
                ],
            }
        ],
    }
    assert "output" not in response.text
    assert "mcp" not in response.text.lower()
    assert "chunk_id" not in response.text


@pytest.mark.asyncio
async def test_assistant_stream_endpoint_returns_native_sse_and_completed_tombstone() -> None:
    service = AssistantService(RecordingSupervisorClient())
    turn_id = uuid4()
    async with _assistant_api_client(service) as client:
        streamed = await client.post(
            "/assistant/messages/stream",
            json={"turn_id": str(turn_id), "message": "Find projects"},
        )
        cancelled = await client.post(f"/assistant/turns/{turn_id}/cancel")

    assert streamed.status_code == 200
    assert streamed.headers["content-type"].startswith("text/event-stream")
    assert "event: result" in streamed.text
    assert "Safe answer" not in streamed.text
    assert "Answer 1" in streamed.text
    assert '"presentation":"text"' in streamed.text
    assert cancelled.status_code == 200
    assert cancelled.json()["outcome"] == "completed"
    assert cancelled.json()["result"]["message"]["content"] == "Answer 1"
    assert cancelled.json()["result"]["message"]["presentation"] == "text"
    assert "mcp" not in cancelled.text.casefold()


@pytest.mark.asyncio
async def test_assistant_stream_endpoint_validation_and_unknown_cancel_are_safe() -> None:
    service = AssistantService(RecordingSupervisorClient())
    async with _assistant_api_client(service) as client:
        invalid = await client.post(
            "/assistant/messages/stream",
            json={"turn_id": str(uuid4()), "message": "   "},
        )
        unknown = await client.post(f"/assistant/turns/{uuid4()}/cancel")

    assert invalid.status_code == 422
    assert unknown.json() == {"outcome": "uncertain", "result": None}


@pytest.mark.asyncio
async def test_assistant_endpoint_validation_dependency_and_uncertain_failure() -> None:
    async with _assistant_api_client() as client:
        missing = await client.post("/assistant/messages", json={"message": "hello"})
    async with _assistant_api_client(FakeAssistantService()) as client:
        invalid = await client.post("/assistant/messages", json={"message": "   "})

    assert missing.status_code == 503
    assert missing.headers.get("X-RepoScout-Completion") is None
    assert invalid.status_code == 422

    service = FakeAssistantService()
    service.error = SupervisorTimeoutError(UNCERTAIN_COMPLETION_MESSAGE, uncertain=True)
    async with _assistant_api_client(service) as client:
        uncertain = await client.post("/assistant/messages", json={"message": "add a note"})

    assert uncertain.status_code == 504
    assert uncertain.headers["X-RepoScout-Completion"] == "uncertain"
    assert uncertain.json()["detail"] == UNCERTAIN_COMPLETION_MESSAGE
