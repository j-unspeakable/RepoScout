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
    AssistantReply,
    AssistantService,
    ConversationConflictError,
    ConversationExpiredError,
    SupervisorBadGatewayError,
    SupervisorClient,
    SupervisorError,
    SupervisorResult,
    SupervisorTimeoutError,
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
        "stream": False,
    }
    await http_client.aclose()


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

        assert caught.value.uncertain is True
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

    assert caught.value.uncertain is True
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

    assert caught.value.uncertain is True
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
async def test_supervisor_client_maps_safe_uncertain_http_errors(
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

    with pytest.raises(error_type, match="couldn't confirm") as caught:
        await client.send([{"role": "user", "content": "save it"}])

    assert caught.value.uncertain is True
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
        client = SupervisorClient(
            Settings(app_env=AppEnvironment.TEST, supervisor_endpoint_name="endpoint"),
            workspace_client=FakeWorkspace(),
            client=httpx.AsyncClient(
                transport=httpx.MockTransport(
                    lambda request, response_payload=payload: httpx.Response(
                        200, json=response_payload
                    )
                )
            ),
        )
        with pytest.raises(SupervisorBadGatewayError) as caught:
            await client.send([{"role": "user", "content": "find projects"}])
        assert caught.value.uncertain is True


@pytest.mark.asyncio
async def test_supervisor_client_timeout_is_uncertain_and_safe() -> None:
    def timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("raw timeout detail", request=request)

    client = SupervisorClient(
        Settings(app_env=AppEnvironment.TEST, supervisor_endpoint_name="endpoint"),
        workspace_client=FakeWorkspace(),
        client=httpx.AsyncClient(transport=httpx.MockTransport(timeout)),
    )

    with pytest.raises(SupervisorTimeoutError) as caught:
        await client.send([{"role": "user", "content": "add a note"}])

    assert str(caught.value) == UNCERTAIN_COMPLETION_MESSAGE
    assert "raw timeout detail" not in str(caught.value)


class RecordingSupervisorClient:
    def __init__(self) -> None:
        self.inputs: list[list[dict[str, Any]]] = []
        self.error: Exception | None = None

    async def send(self, input_items: list[dict[str, Any]]) -> SupervisorResult:
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

    async def send(self, input_items: list[dict[str, Any]]) -> SupervisorResult:
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

    async def send(self, input_items: list[dict[str, Any]]) -> SupervisorResult:
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


class FakeAssistantService:
    def __init__(self) -> None:
        self.error: Exception | None = None

    async def send(self, conversation_id: UUID | None, message: str) -> AssistantReply:
        if self.error:
            raise self.error
        return AssistantReply(conversation_id=conversation_id or uuid4(), content="Safe answer")


@asynccontextmanager
async def _assistant_api_client(
    service: FakeAssistantService | None = None,
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
    async with _assistant_api_client(FakeAssistantService()) as client:
        response = await client.post("/assistant/messages", json={"message": "  Find projects  "})

    assert response.status_code == 200
    assert set(response.json()) == {"conversation_id", "message"}
    assert response.json()["message"] == {"role": "assistant", "content": "Safe answer"}
    assert "output" not in response.text
    assert "mcp" not in response.text.lower()


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
