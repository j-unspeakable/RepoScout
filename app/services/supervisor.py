import asyncio
import json
from collections import OrderedDict
from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass, field
from time import monotonic
from typing import Any, Protocol, cast
from uuid import UUID, uuid4

import httpx
from asyncer import asyncify
from databricks.sdk import WorkspaceClient

from app.config import Settings

UNCERTAIN_COMPLETION_MESSAGE = (
    "RepoScout couldn't confirm the final response. A requested action may already have "
    "completed. Check My Projects before retrying."
)


class SupervisorError(RuntimeError):
    """A safe boundary failure for the Supervisor serving endpoint."""

    def __init__(self, message: str, *, uncertain: bool = False) -> None:
        super().__init__(message)
        self.uncertain = uncertain


class SupervisorUnavailableError(SupervisorError):
    def __init__(
        self,
        message: str,
        *,
        retry_after: int | None = None,
        uncertain: bool = False,
    ) -> None:
        super().__init__(message, uncertain=uncertain)
        self.retry_after = retry_after


class SupervisorTimeoutError(SupervisorError):
    pass


class SupervisorBadGatewayError(SupervisorError):
    pass


class ConversationExpiredError(RuntimeError):
    pass


class ConversationConflictError(RuntimeError):
    pass


class ConversationCapacityError(RuntimeError):
    pass


class WorkspaceConfigProtocol(Protocol):
    host: str

    def authenticate(self) -> dict[str, str]: ...


class WorkspaceClientProtocol(Protocol):
    @property
    def config(self) -> WorkspaceConfigProtocol: ...


@dataclass(frozen=True, slots=True)
class SupervisorResult:
    answer: str
    output_items: list[dict[str, Any]]


class SupervisorClientProtocol(Protocol):
    async def send(self, input_items: list[dict[str, Any]]) -> SupervisorResult: ...


class SupervisorClient:
    _MAX_RESPONSE_BYTES = 512_000

    def __init__(
        self,
        settings: Settings,
        workspace_client: WorkspaceClientProtocol | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        endpoint_name = settings.supervisor_endpoint_name
        if endpoint_name is None or not endpoint_name.strip():
            raise ValueError("SUPERVISOR_ENDPOINT_NAME is required")
        self._endpoint_name = endpoint_name.strip()
        self._profile = settings.databricks_config_profile
        self._workspace = workspace_client
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(settings.supervisor_request_timeout_seconds, connect=10.0)
        )

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def send(self, input_items: list[dict[str, Any]]) -> SupervisorResult:
        try:
            url, headers = await asyncify(self._request_context)()
        except Exception as exc:
            raise SupervisorUnavailableError("Ask RepoScout authentication is unavailable") from exc

        headers = {**headers, "Accept": "application/json", "Content-Type": "application/json"}
        try:
            response = await self._client.post(
                url,
                headers=headers,
                json={
                    "model": self._endpoint_name,
                    "input": input_items,
                    "stream": False,
                },
            )
        except httpx.TimeoutException as exc:
            raise SupervisorTimeoutError(
                UNCERTAIN_COMPLETION_MESSAGE,
                uncertain=True,
            ) from exc
        except httpx.RequestError as exc:
            raise SupervisorBadGatewayError(
                UNCERTAIN_COMPLETION_MESSAGE,
                uncertain=True,
            ) from exc

        retry_after = self._retry_after(response)
        if len(response.content) > self._MAX_RESPONSE_BYTES:
            raise SupervisorBadGatewayError(
                UNCERTAIN_COMPLETION_MESSAGE,
                uncertain=True,
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise SupervisorBadGatewayError(
                UNCERTAIN_COMPLETION_MESSAGE,
                uncertain=True,
            ) from exc
        if not isinstance(payload, dict):
            raise SupervisorBadGatewayError(
                UNCERTAIN_COMPLETION_MESSAGE,
                uncertain=True,
            )
        if not response.is_success or isinstance(payload.get("error"), dict):
            self._raise_http_error(response.status_code, retry_after)
        if payload.get("status") != "completed":
            raise SupervisorBadGatewayError(
                UNCERTAIN_COMPLETION_MESSAGE,
                uncertain=True,
            )

        output = payload.get("output")
        if not isinstance(output, list) or not all(isinstance(item, dict) for item in output):
            raise SupervisorBadGatewayError(
                UNCERTAIN_COMPLETION_MESSAGE,
                uncertain=True,
            )
        answer = self._assistant_text(output)
        if not answer:
            raise SupervisorBadGatewayError(
                UNCERTAIN_COMPLETION_MESSAGE,
                uncertain=True,
            )
        return SupervisorResult(answer=answer, output_items=deepcopy(output))

    def _request_context(self) -> tuple[str, dict[str, str]]:
        if self._workspace is None:
            self._workspace = cast(
                WorkspaceClientProtocol,
                WorkspaceClient(profile=self._profile),
            )
        host = self._workspace.config.host
        if not isinstance(host, str) or not host.strip():
            raise ValueError("Databricks workspace host is unavailable")
        url = f"{host.rstrip('/')}/serving-endpoints/responses"
        return url, dict(self._workspace.config.authenticate())

    @staticmethod
    def _assistant_text(output: list[dict[str, Any]]) -> str:
        texts: list[str] = []
        for item in output:
            if item.get("type") != "message" or item.get("role") != "assistant":
                continue
            content = item.get("content")
            if not isinstance(content, list):
                continue
            for part in content:
                if not isinstance(part, dict) or part.get("type") not in {"output_text", "text"}:
                    continue
                text = part.get("text")
                if isinstance(text, str) and text.strip():
                    texts.append(text.strip())
        return "\n".join(texts).strip()

    @staticmethod
    def _retry_after(response: httpx.Response) -> int | None:
        value = response.headers.get("Retry-After")
        return int(value) if value and value.isdigit() and int(value) > 0 else None

    @staticmethod
    def _raise_http_error(status_code: int, retry_after: int | None) -> None:
        if status_code in {408, 504}:
            raise SupervisorTimeoutError(UNCERTAIN_COMPLETION_MESSAGE, uncertain=True)
        if status_code in {429, 502, 503}:
            raise SupervisorUnavailableError(
                UNCERTAIN_COMPLETION_MESSAGE,
                retry_after=retry_after,
                uncertain=True,
            )
        raise SupervisorBadGatewayError(UNCERTAIN_COMPLETION_MESSAGE, uncertain=True)


@dataclass(slots=True)
class ConversationTurn:
    user_message: dict[str, Any]
    output_items: list[dict[str, Any]]


@dataclass(slots=True)
class ConversationState:
    turns: list[ConversationTurn] = field(default_factory=list)
    last_accessed: float = 0.0
    busy: bool = False


@dataclass(frozen=True, slots=True)
class AssistantReply:
    conversation_id: UUID
    content: str


class AssistantService:
    def __init__(
        self,
        client: SupervisorClientProtocol,
        *,
        max_turns: int = 12,
        ttl_seconds: float = 3600.0,
        max_conversations: int = 100,
        max_history_bytes: int = 1_000_000,
        clock: Callable[[], float] = monotonic,
        id_factory: Callable[[], UUID] = uuid4,
    ) -> None:
        self._client = client
        self._max_turns = max_turns
        self._ttl_seconds = ttl_seconds
        self._max_conversations = max_conversations
        self._max_history_bytes = max_history_bytes
        self._clock = clock
        self._id_factory = id_factory
        self._conversations: OrderedDict[UUID, ConversationState] = OrderedDict()
        self._state_lock = asyncio.Lock()

    async def send(self, conversation_id: UUID | None, message: str) -> AssistantReply:
        identifier, state, is_new = await self._begin_turn(conversation_id)
        user_message: dict[str, Any] = {
            "type": "message",
            "role": "user",
            "content": message,
        }
        input_items = self._flatten_history(state.turns)
        input_items.append(deepcopy(user_message))

        try:
            result = await self._client.send(input_items)
        except asyncio.CancelledError:
            await self._finish_failed_turn(identifier, state, is_new)
            raise
        except Exception:
            await self._finish_failed_turn(identifier, state, is_new)
            raise

        async with self._state_lock:
            current = self._conversations.get(identifier)
            if current is state:
                state.turns.append(
                    ConversationTurn(
                        user_message=deepcopy(user_message),
                        output_items=deepcopy(result.output_items),
                    )
                )
                state.turns = state.turns[-self._max_turns :]
                while (
                    len(state.turns) > 1
                    and self._history_size(state.turns) > self._max_history_bytes
                ):
                    state.turns.pop(0)
                state.last_accessed = self._clock()
                state.busy = False
                self._conversations.move_to_end(identifier)
        return AssistantReply(conversation_id=identifier, content=result.answer)

    async def _begin_turn(
        self, conversation_id: UUID | None
    ) -> tuple[UUID, ConversationState, bool]:
        async with self._state_lock:
            now = self._clock()
            self._remove_expired(now)
            if conversation_id is not None:
                state = self._conversations.get(conversation_id)
                if state is None:
                    raise ConversationExpiredError("Conversation expired; start a new conversation")
                if state.busy:
                    raise ConversationConflictError("A response is already in progress")
                state.busy = True
                state.last_accessed = now
                self._conversations.move_to_end(conversation_id)
                return conversation_id, state, False

            self._make_capacity()
            identifier = self._id_factory()
            state = ConversationState(last_accessed=now, busy=True)
            self._conversations[identifier] = state
            return identifier, state, True

    async def _finish_failed_turn(
        self, identifier: UUID, state: ConversationState, is_new: bool
    ) -> None:
        async with self._state_lock:
            current = self._conversations.get(identifier)
            if current is not state:
                return
            if is_new and not state.turns:
                del self._conversations[identifier]
            else:
                state.busy = False
                state.last_accessed = self._clock()

    def _remove_expired(self, now: float) -> None:
        expired = [
            identifier
            for identifier, state in self._conversations.items()
            if not state.busy and now - state.last_accessed >= self._ttl_seconds
        ]
        for identifier in expired:
            del self._conversations[identifier]

    def _make_capacity(self) -> None:
        if len(self._conversations) < self._max_conversations:
            return
        for identifier, state in self._conversations.items():
            if not state.busy:
                del self._conversations[identifier]
                return
        raise ConversationCapacityError("Ask RepoScout is temporarily at capacity")

    @staticmethod
    def _flatten_history(turns: list[ConversationTurn]) -> list[dict[str, Any]]:
        history: list[dict[str, Any]] = []
        for turn in turns:
            history.append(deepcopy(turn.user_message))
            history.extend(deepcopy(turn.output_items))
        return history

    @staticmethod
    def _history_size(turns: list[ConversationTurn]) -> int:
        return len(
            json.dumps(
                [
                    {"user_message": turn.user_message, "output_items": turn.output_items}
                    for turn in turns
                ],
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
        )
