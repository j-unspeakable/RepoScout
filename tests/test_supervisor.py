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
