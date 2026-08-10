import asyncio
import json
import re
from collections import OrderedDict
from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass, field
from time import monotonic
from typing import Any, Protocol, cast
from urllib.parse import urlsplit
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
class AssistantEvidenceChunk:
    chunk_index: int
    chunk_text: str


@dataclass(frozen=True, slots=True)
class AssistantEvidenceProject:
    repo_id: int
    full_name: str
    html_url: str
    evidence: tuple[AssistantEvidenceChunk, ...]


@dataclass(frozen=True, slots=True)
class SupervisorResult:
    answer: str
    output_items: list[dict[str, Any]]
    evidence: tuple[AssistantEvidenceProject, ...] = ()


class SupervisorClientProtocol(Protocol):
    async def send(self, input_items: list[dict[str, Any]]) -> SupervisorResult: ...


class SupervisorClient:
    """Call the Supervisor endpoint and safely replay bounded MCP approval rounds.

    Only the five allowlisted RepoScout tools are approved. Intermediate response items remain
    server-side and are returned to ``AssistantService`` solely for stateless endpoint replay.
    """

    _MAX_RESPONSE_BYTES = 512_000
    _MAX_MCP_APPROVAL_ROUNDS = 8
    _MAX_EVIDENCE_PROJECTS = 10
    _MAX_EVIDENCE_CHUNKS = 5
    _MAX_EVIDENCE_TEXT_LENGTH = 4_000
    _ALLOWED_MCP_TOOLS = frozenset(
        {
            "search_projects",
            "get_project_details",
            "save_project",
            "update_project_status",
            "add_project_note",
        }
    )

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
        request_input = deepcopy(input_items)
        replay_items: list[dict[str, Any]] = []
        approval_rounds = 0

        while True:
            output = await self._send_once(
                request_input,
                uncertain_before_request=bool(replay_items),
            )
            approval_responses = self._approval_responses(output)
            if approval_responses:
                if approval_rounds >= self._MAX_MCP_APPROVAL_ROUNDS:
                    raise SupervisorBadGatewayError(
                        UNCERTAIN_COMPLETION_MESSAGE,
                        uncertain=True,
                    )
                approval_rounds += 1
                continuation_items = self._interleave_approval_responses(
                    output,
                    approval_responses,
                )
                replay_items.extend(deepcopy(continuation_items))
                request_input.extend(deepcopy(continuation_items))
                continue

            replay_items.extend(deepcopy(output))
            if approval_rounds and not any(
                item.get("type") == "function_call_output" for item in output
            ):
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
            evidence = self._extract_evidence(replay_items)
            return SupervisorResult(
                answer=answer,
                output_items=replay_items,
                evidence=self._evidence_referenced_by_answer(answer, evidence),
            )

    async def _send_once(
        self,
        input_items: list[dict[str, Any]],
        *,
        uncertain_before_request: bool,
    ) -> list[dict[str, Any]]:
        try:
            url, headers = await asyncify(self._request_context)()
        except Exception as exc:
            if uncertain_before_request:
                raise SupervisorUnavailableError(
                    UNCERTAIN_COMPLETION_MESSAGE,
                    uncertain=True,
                ) from exc
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
        return output

    @classmethod
    def _approval_responses(cls, output: list[dict[str, Any]]) -> list[dict[str, Any]]:
        responses: list[dict[str, Any]] = []
        identifiers: set[str] = set()
        for item in output:
            if item.get("type") != "mcp_approval_request":
                continue
            identifier = item.get("id")
            tool_name = item.get("name")
            if (
                not isinstance(identifier, str)
                or not identifier.strip()
                or identifier in identifiers
                or tool_name not in cls._ALLOWED_MCP_TOOLS
            ):
                raise SupervisorBadGatewayError(
                    UNCERTAIN_COMPLETION_MESSAGE,
                    uncertain=True,
                )
            identifiers.add(identifier)
            responses.append(
                {
                    "type": "mcp_approval_response",
                    "id": identifier,
                    "approval_request_id": identifier,
                    "approve": True,
                }
            )
        return responses

    @staticmethod
    def _interleave_approval_responses(
        output: list[dict[str, Any]],
        responses: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        responses_by_request = {response["approval_request_id"]: response for response in responses}
        continuation: list[dict[str, Any]] = []
        for item in output:
            continuation.append(item)
            if item.get("type") == "mcp_approval_request":
                continuation.append(responses_by_request[item["id"]])
        return continuation

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

    @classmethod
    def _extract_evidence(
        cls,
        output_items: list[dict[str, Any]],
    ) -> tuple[AssistantEvidenceProject, ...]:
        projects: OrderedDict[int, dict[str, Any]] = OrderedDict()
        for item in output_items:
            if item.get("type") not in {"function_call_output", "mcp_call"}:
                continue
            for payload in cls._decoded_tool_payloads(item.get("output")):
                candidates = payload.get("projects")
                if isinstance(candidates, list):
                    project_values = candidates
                elif "repo_id" in payload and "evidence" in payload:
                    project_values = [payload]
                else:
                    continue
                for value in project_values:
                    cls._collect_evidence_project(projects, value)
                    if len(projects) >= cls._MAX_EVIDENCE_PROJECTS:
                        break
            if len(projects) >= cls._MAX_EVIDENCE_PROJECTS:
                break

        return tuple(
            AssistantEvidenceProject(
                repo_id=project["repo_id"],
                full_name=project["full_name"],
                html_url=project["html_url"],
                evidence=tuple(project["evidence"]),
            )
            for project in projects.values()
            if project["evidence"]
        )

    @staticmethod
    def _evidence_referenced_by_answer(
        answer: str,
        evidence: tuple[AssistantEvidenceProject, ...],
    ) -> tuple[AssistantEvidenceProject, ...]:
        normalized_answer = answer.casefold()
        referenced: list[AssistantEvidenceProject] = []
        for project in evidence:
            full_name = project.full_name.casefold()
            repository_id_pattern = rf"\brepo[_\s-]*id\s*[:=]\s*{project.repo_id}\b"
            if (
                full_name in normalized_answer
                or project.html_url.casefold() in normalized_answer
                or re.search(repository_id_pattern, normalized_answer) is not None
            ):
                referenced.append(project)
        return tuple(referenced)

    @classmethod
    def _decoded_tool_payloads(cls, value: Any, depth: int = 0) -> list[dict[str, Any]]:
        if depth > 2:
            return []
        if isinstance(value, str):
            if not value.strip() or len(value) > cls._MAX_RESPONSE_BYTES:
                return []
            try:
                decoded = json.loads(value)
            except (TypeError, ValueError):
                return []
            return cls._decoded_tool_payloads(decoded, depth + 1)
        if isinstance(value, dict):
            payloads = [value]
            for key in ("structuredContent", "structured_content"):
                payloads.extend(cls._decoded_tool_payloads(value.get(key), depth + 1))
            content = value.get("content")
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict):
                        payloads.extend(cls._decoded_tool_payloads(block.get("text"), depth + 1))
            return payloads
        if isinstance(value, list):
            payloads: list[dict[str, Any]] = []
            for item in value:
                payloads.extend(cls._decoded_tool_payloads(item, depth + 1))
            return payloads
        return []

    @classmethod
    def _collect_evidence_project(
        cls,
        projects: OrderedDict[int, dict[str, Any]],
        value: Any,
    ) -> None:
        if not isinstance(value, dict):
            return
        repo_id = value.get("repo_id")
        full_name = value.get("full_name")
        html_url = value.get("html_url")
        evidence = value.get("evidence")
        if (
            not isinstance(repo_id, int)
            or isinstance(repo_id, bool)
            or repo_id <= 0
            or not isinstance(full_name, str)
            or not full_name.strip()
            or len(full_name) > 200
            or not isinstance(html_url, str)
            or not cls._valid_github_repository_url(html_url, full_name)
            or not isinstance(evidence, list)
        ):
            return

        project = projects.setdefault(
            repo_id,
            {
                "repo_id": repo_id,
                "full_name": full_name.strip(),
                "html_url": html_url,
                "evidence": [],
                "chunk_indexes": set(),
            },
        )
        for chunk in evidence:
            if len(project["evidence"]) >= cls._MAX_EVIDENCE_CHUNKS:
                break
            if not isinstance(chunk, dict):
                continue
            chunk_index = chunk.get("chunk_index")
            chunk_text = chunk.get("chunk_text")
            if (
                not isinstance(chunk_index, int)
                or isinstance(chunk_index, bool)
                or chunk_index < 0
                or chunk_index in project["chunk_indexes"]
                or not isinstance(chunk_text, str)
                or not chunk_text.strip()
                or len(chunk_text) > cls._MAX_EVIDENCE_TEXT_LENGTH
            ):
                continue
            project["chunk_indexes"].add(chunk_index)
            project["evidence"].append(
                AssistantEvidenceChunk(
                    chunk_index=chunk_index,
                    chunk_text=chunk_text.strip(),
                )
            )

    @staticmethod
    def _valid_github_repository_url(html_url: str, full_name: str) -> bool:
        try:
            parsed = urlsplit(html_url)
            segments = [segment for segment in parsed.path.split("/") if segment]
            return (
                parsed.scheme == "https"
                and parsed.hostname is not None
                and parsed.hostname.casefold() == "github.com"
                and parsed.username is None
                and parsed.password is None
                and parsed.port is None
                and not parsed.query
                and not parsed.fragment
                and len(segments) == 2
                and "/".join(segments).casefold() == full_name.strip().casefold()
            )
        except ValueError:
            return False

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
    evidence: tuple[AssistantEvidenceProject, ...] = ()


class AssistantService:
    """Keep bounded, process-local Supervisor history behind opaque conversation IDs."""

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
        return AssistantReply(
            conversation_id=identifier,
            content=result.answer,
            evidence=result.evidence,
        )

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
