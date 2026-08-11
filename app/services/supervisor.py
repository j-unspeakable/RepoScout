import asyncio
import json
import math
import re
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from copy import deepcopy
from dataclasses import dataclass, field
from enum import StrEnum
from time import monotonic
from typing import Any, NoReturn, Protocol, cast
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


class AssistantTurnConflictError(RuntimeError):
    pass


class SupervisorProgressPhase(StrEnum):
    WORKING = "working"
    SEARCHING_PROJECTS = "searching_projects"
    REVIEWING_DETAILS = "reviewing_details"
    SAVING_PROJECTS = "saving_projects"
    UPDATING_STATUS = "updating_status"
    ADDING_NOTES = "adding_notes"
    CONTINUING = "continuing"
    FINISHING = "finishing"


class AssistantPresentation(StrEnum):
    CARDS = "cards"
    REFERENCES = "references"
    TEXT = "text"


class AssistantCancellationOutcome(StrEnum):
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    UNCERTAIN = "uncertain"


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
    similarity: float | None = None


@dataclass(frozen=True, slots=True)
class AssistantEvidenceProject:
    repo_id: int
    name: str
    full_name: str
    owner: str
    description: str | None
    html_url: str
    primary_language: str | None
    stars: int
    forks: int
    open_issues: int
    topics: tuple[str, ...]
    license: str | None
    similarity: float | None
    evidence: tuple[AssistantEvidenceChunk, ...]


@dataclass(frozen=True, slots=True)
class SupervisorResult:
    answer: str
    output_items: list[dict[str, Any]]
    evidence: tuple[AssistantEvidenceProject, ...] = ()
    presentation: AssistantPresentation = AssistantPresentation.TEXT


ProgressCallback = Callable[[SupervisorProgressPhase], Awaitable[None]]


@dataclass(slots=True)
class SupervisorTurnControl:
    """Synchronize cancellation with the exact write-approval dispatch boundary."""

    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    cancellation_requested: bool = False
    write_may_have_executed: bool = False

    async def prepare_followup(self, tool_names: list[str], write_tools: frozenset[str]) -> None:
        async with self.lock:
            if self.cancellation_requested:
                raise asyncio.CancelledError
            if any(name in write_tools for name in tool_names):
                self.write_may_have_executed = True

    async def request_cancellation(self) -> bool:
        async with self.lock:
            self.cancellation_requested = True
            return self.write_may_have_executed

    async def ensure_active(self) -> None:
        async with self.lock:
            if self.cancellation_requested:
                raise asyncio.CancelledError


class SupervisorClientProtocol(Protocol):
    async def send(
        self,
        input_items: list[dict[str, Any]],
        *,
        progress: ProgressCallback | None = None,
        control: SupervisorTurnControl | None = None,
    ) -> SupervisorResult: ...


class SupervisorClient:
    """Call the Supervisor endpoint and safely replay bounded MCP approval rounds.

    Only the five allowlisted RepoScout tools are approved. Intermediate response items remain
    server-side and are returned to ``AssistantService`` solely for stateless endpoint replay.
    """

    _MAX_RESPONSE_BYTES = 512_000
    _MAX_ACCUMULATED_RESPONSE_BYTES = 1_000_000
    _MAX_SUPERVISOR_CYCLES = 48
    _MAX_MCP_APPROVAL_ROUNDS = 32
    _MAX_TASK_CONTINUATIONS = 8
    _MAX_EVIDENCE_PROJECTS = 10
    _MAX_EVIDENCE_CHUNKS = 5
    _MAX_EVIDENCE_TEXT_LENGTH = 4_000
    _MAX_DESCRIPTION_LENGTH = 2_000
    _MAX_TOPICS = 8
    _PROSE_CHARACTER_GUIDELINE = 1_200
    _PROSE_WORD_GUIDELINE = 180
    _SINGLE_PROJECT_FALLBACK = (
        "Here’s the relevant project RepoScout identified. Review its README evidence below, "
        "then ask me to explain it in more depth, save it, update its status, or add a note."
    )
    _MULTIPLE_PROJECT_FALLBACK = (
        "Here are the relevant projects RepoScout found. Review their README evidence below, "
        "then ask me to compare options, explain a project in more depth, save promising "
        "choices, update their status, or add notes."
    )
    _GITHUB_URL_PATTERN = re.compile(
        r"https://github\.com/[^\s)\]}>,;]+",
        flags=re.IGNORECASE,
    )
    _MARKDOWN_GITHUB_LINK_PATTERN = re.compile(
        r"\[([^\]\n]+)\]\((https://github\.com/[^)\s]+)\)",
        flags=re.IGNORECASE,
    )
    _REPOSITORY_ID_PATTERN = re.compile(
        r"\brepo(?:sitory)?[_\s-]*id\s*[:=#-]?\s*\d+\b",
        flags=re.IGNORECASE,
    )
    _REPOSITORY_ID_CONTEXT_PATTERN = re.compile(
        r"\b(?:has|with)\s+repo(?:sitory)?[_\s-]*id\s*[:=#-]?\s*\d+\s*"
        r"(?:,?\s*and\s+)?",
        flags=re.IGNORECASE,
    )
    _NUMERIC_METADATA_PATTERN = re.compile(
        r"\b(?:\d[\d,.]*|\d+(?:\.\d+)?[km])\s+"
        r"(?:stars?|forks?|open[\s_-]+issues?)\b",
        flags=re.IGNORECASE,
    )
    _RAW_SIMILARITY_PATTERN = re.compile(
        r"(?:\bsimilarity\s*[:=]\s*-?\d+(?:\.\d+)?\b|"
        r"\b-?\d+(?:\.\d+)?\s+similarity(?:\s+score)?\b)",
        flags=re.IGNORECASE,
    )
    _RAW_CHUNK_REFERENCE_PATTERN = re.compile(
        r"\bchunks?\s+\d+(?:\s*[-–]\s*\d+)?\b",
        flags=re.IGNORECASE,
    )
    _LIST_LINE_PATTERN = re.compile(r"^\s*(?:[-*•]|\d+[.)])\s+")
    _EMPTY_LIST_LINE_PATTERN = re.compile(
        r"^\s*(?:[-*•]|\d+[.)])\s*$",
        flags=re.MULTILINE,
    )
    _PARENTHETICAL_REPOSITORY_ID_PATTERN = re.compile(
        r"\s*\(\s*repo(?:sitory)?[_\s-]*id\s*[:=#-]?\s*\d+\s*\)",
        flags=re.IGNORECASE,
    )
    _EVIDENCE_INTENT_PATTERN = re.compile(
        r"\b(?:readme\s+evidence|supporting\s+evidence|why\s+this\s+matched|"
        r"citations?|sources?|show\s+(?:me\s+)?(?:the\s+)?evidence)\b",
        flags=re.IGNORECASE,
    )
    _WRITE_INTENT_PATTERN = re.compile(
        r"\b(?:save|bookmark)\b|\b(?:add|create|write)\s+(?:a\s+)?note\b|"
        r"\b(?:mark|set|update|change|label)\b[\s\S]{0,500}\b(?:status|interested|to\s+try|"
        r"in\s+progress|completed)\b",
        flags=re.IGNORECASE,
    )
    _COMPARISON_DETAIL_INTENT_PATTERN = re.compile(
        r"\b(?:compare|comparison|versus|vs\.?|differences?|trade[ -]?offs?|"
        r"pros\s+and\s+cons|evaluate|evaluation|better\s+fit|best\s+fit|"
        r"tell\s+me\s+more|more\s+(?:info|information|details)|project\s+details|"
        r"explain|what\s+makes|how\s+does|which\s+(?:one|of\s+these|of\s+them|is|would|should))\b",
        flags=re.IGNORECASE,
    )
    _SEARCH_LIST_INTENT_PATTERN = re.compile(
        r"\b(?:find|search|list|recommend|recommendations?|suggest|suggestions?|"
        r"show\s+me|resources?|projects?|repositories?)\b",
        flags=re.IGNORECASE,
    )
    _READ_TOOLS = frozenset({"search_projects", "get_project_details"})
    _WRITE_TOOLS = frozenset({"save_project", "update_project_status", "add_project_note"})
    _TOOL_PROGRESS = {
        "search_projects": SupervisorProgressPhase.SEARCHING_PROJECTS,
        "get_project_details": SupervisorProgressPhase.REVIEWING_DETAILS,
        "save_project": SupervisorProgressPhase.SAVING_PROJECTS,
        "update_project_status": SupervisorProgressPhase.UPDATING_STATUS,
        "add_project_note": SupervisorProgressPhase.ADDING_NOTES,
    }
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
        self._cycle_timeout_seconds = settings.supervisor_request_timeout_seconds
        self._task_timeout_seconds = settings.supervisor_task_timeout_seconds
        self._workspace = workspace_client
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient()

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def send(
        self,
        input_items: list[dict[str, Any]],
        *,
        progress: ProgressCallback | None = None,
        control: SupervisorTurnControl | None = None,
    ) -> SupervisorResult:
        request_input = deepcopy(input_items)
        replay_items: list[dict[str, Any]] = []
        turn_control = control or SupervisorTurnControl()
        approval_count = 0
        continuation_count = 0
        seen_control_ids: set[str] = set()
        pending_tool_outputs: set[str] = set()
        last_continuation_step: int | None = None
        accumulated_bytes = 0
        deadline = monotonic() + self._task_timeout_seconds
        last_progress: SupervisorProgressPhase | None = None

        async def emit(phase: SupervisorProgressPhase) -> None:
            nonlocal last_progress
            if progress is None or phase is last_progress:
                return
            last_progress = phase
            await progress(phase)

        await emit(SupervisorProgressPhase.WORKING)
        for _cycle in range(self._MAX_SUPERVISOR_CYCLES):
            await turn_control.ensure_active()
            remaining = deadline - monotonic()
            if remaining <= 0:
                self._raise_timeout(turn_control)
            output = await self._send_once(
                request_input,
                timeout_seconds=min(self._cycle_timeout_seconds, remaining),
                control=turn_control,
            )
            encoded_size = len(
                json.dumps(output, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            )
            accumulated_bytes += encoded_size
            if accumulated_bytes > self._MAX_ACCUMULATED_RESPONSE_BYTES:
                self._raise_bad_gateway(turn_control)

            for item in output:
                if item.get("type") != "function_call_output":
                    continue
                call_id = item.get("call_id")
                if isinstance(call_id, str):
                    pending_tool_outputs.discard(call_id)

            try:
                approval_responses = self._approval_responses(output)
                continuation_responses, last_continuation_step = self._continuation_responses(
                    output,
                    last_continuation_step,
                )
            except ValueError:
                self._raise_bad_gateway(turn_control)
            control_responses = [*approval_responses, *continuation_responses]
            control_ids = [
                value
                for response in control_responses
                if isinstance(
                    value := response.get("approval_request_id")
                    or response.get("continue_request_id"),
                    str,
                )
            ]
            if len(control_ids) != len(control_responses) or any(
                identifier in seen_control_ids for identifier in control_ids
            ):
                self._raise_bad_gateway(turn_control)
            seen_control_ids.update(control_ids)

            if approval_responses or continuation_responses:
                approval_count += len(approval_responses)
                continuation_count += len(continuation_responses)
                if (
                    approval_count > self._MAX_MCP_APPROVAL_ROUNDS
                    or continuation_count > self._MAX_TASK_CONTINUATIONS
                ):
                    self._raise_bad_gateway(turn_control)
                tool_names = [
                    item["name"]
                    for item in output
                    if item.get("type") == "mcp_approval_request"
                    and isinstance(item.get("name"), str)
                ]
                await turn_control.prepare_followup(tool_names, self._WRITE_TOOLS)
                phases = {
                    self._TOOL_PROGRESS[name] for name in tool_names if name in self._TOOL_PROGRESS
                }
                if continuation_responses or len(phases) > 1:
                    await emit(SupervisorProgressPhase.CONTINUING)
                elif phases:
                    await emit(next(iter(phases)))
                pending_tool_outputs.update(
                    response["approval_request_id"] for response in approval_responses
                )
                continuation_items = self._interleave_control_responses(output, control_responses)
                replay_items.extend(deepcopy(continuation_items))
                request_input.extend(deepcopy(continuation_items))
                continue

            replay_items.extend(deepcopy(output))
            if pending_tool_outputs:
                self._raise_bad_gateway(turn_control)
            answer = self._assistant_text(output)
            if not answer:
                self._raise_bad_gateway(turn_control)
            await emit(SupervisorProgressPhase.FINISHING)
            evidence = self._select_visible_projects(answer, input_items, replay_items)
            presentation = self._select_presentation(
                self._latest_user_message(input_items),
                evidence,
                replay_items,
            )
            visible_evidence = evidence if presentation is not AssistantPresentation.TEXT else ()
            return SupervisorResult(
                answer=self._normalize_visible_answer(
                    answer,
                    visible_evidence,
                    presentation,
                ),
                output_items=replay_items,
                evidence=visible_evidence,
                presentation=presentation,
            )
        self._raise_bad_gateway(turn_control)

    async def _send_once(
        self,
        input_items: list[dict[str, Any]],
        *,
        timeout_seconds: float,
        control: SupervisorTurnControl,
    ) -> list[dict[str, Any]]:
        try:
            url, headers = await asyncify(self._request_context)()
        except Exception as exc:
            raise SupervisorUnavailableError(
                self._failure_message(control, "Ask RepoScout authentication is unavailable"),
                uncertain=control.write_may_have_executed,
            ) from exc

        await control.ensure_active()
        headers = {
            **headers,
            "Accept": "text/event-stream",
            "Content-Type": "application/json",
        }
        try:
            async with self._client.stream(
                "POST",
                url,
                headers=headers,
                json={
                    "model": self._endpoint_name,
                    "input": input_items,
                    "stream": True,
                    "databricks_options": {"long_task": True},
                },
                timeout=httpx.Timeout(timeout_seconds, connect=min(10.0, timeout_seconds)),
            ) as response:
                retry_after = self._retry_after(response)
                if not response.is_success:
                    self._raise_http_error(response.status_code, retry_after, control)
                content_type = response.headers.get("Content-Type", "").casefold()
                if "text/event-stream" in content_type:
                    return await self._read_sse_output(response, control)
                return await self._read_json_output(response, control)
        except httpx.TimeoutException as exc:
            raise SupervisorTimeoutError(
                self._failure_message(control, "Ask RepoScout timed out. Please try again."),
                uncertain=control.write_may_have_executed,
            ) from exc
        except httpx.RequestError as exc:
            raise SupervisorBadGatewayError(
                self._failure_message(control, "Ask RepoScout could not complete the request"),
                uncertain=control.write_may_have_executed,
            ) from exc

    async def _read_json_output(
        self,
        response: httpx.Response,
        control: SupervisorTurnControl,
    ) -> list[dict[str, Any]]:
        content = await self._read_bounded_body(response, control)
        try:
            payload = json.loads(content)
        except ValueError as exc:
            raise SupervisorBadGatewayError(
                self._failure_message(control, "Ask RepoScout returned an invalid response"),
                uncertain=control.write_may_have_executed,
            ) from exc
        if not isinstance(payload, dict):
            self._raise_bad_gateway(control)
        if isinstance(payload.get("error"), dict) or payload.get("status") != "completed":
            self._raise_bad_gateway(control)

        output = payload.get("output")
        if not isinstance(output, list) or not all(isinstance(item, dict) for item in output):
            self._raise_bad_gateway(control)
        return output

    async def _read_sse_output(
        self,
        response: httpx.Response,
        control: SupervisorTurnControl,
    ) -> list[dict[str, Any]]:
        output: list[dict[str, Any]] = []
        data_lines: list[str] = []
        received_bytes = 0

        async def consume_event() -> None:
            if not data_lines:
                return
            raw = "\n".join(data_lines)
            data_lines.clear()
            if raw == "[DONE]":
                return
            try:
                event = json.loads(raw)
            except ValueError as exc:
                raise SupervisorBadGatewayError(
                    self._failure_message(control, "Ask RepoScout returned an invalid response"),
                    uncertain=control.write_may_have_executed,
                ) from exc
            if not isinstance(event, dict):
                self._raise_bad_gateway(control)
            event_type = event.get("type")
            if event_type == "response.output_item.done":
                item = event.get("item")
                if not isinstance(item, dict):
                    self._raise_bad_gateway(control)
                output.append(item)
            elif event_type in {"response.failed", "response.incomplete", "error"}:
                self._raise_bad_gateway(control)

        async for line in response.aiter_lines():
            received_bytes += len(line.encode("utf-8")) + 1
            if received_bytes > self._MAX_RESPONSE_BYTES:
                self._raise_bad_gateway(control)
            if line == "":
                await consume_event()
            elif line.startswith("data:"):
                data_lines.append(line[5:].lstrip())
        await consume_event()
        if not output:
            self._raise_bad_gateway(control)
        return output

    async def _read_bounded_body(
        self,
        response: httpx.Response,
        control: SupervisorTurnControl,
    ) -> bytes:
        chunks: list[bytes] = []
        size = 0
        async for chunk in response.aiter_bytes():
            size += len(chunk)
            if size > self._MAX_RESPONSE_BYTES:
                self._raise_bad_gateway(control)
            chunks.append(chunk)
        return b"".join(chunks)

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
                raise ValueError("Invalid MCP approval request")
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

    @classmethod
    def _continuation_responses(
        cls,
        output: list[dict[str, Any]],
        previous_step: int | None,
    ) -> tuple[list[dict[str, Any]], int | None]:
        responses: list[dict[str, Any]] = []
        last_step = previous_step
        identifiers: set[str] = set()
        for item in output:
            if item.get("type") != "task_continue_request":
                continue
            identifier = item.get("id")
            step = item.get("step")
            if (
                not isinstance(identifier, str)
                or not identifier.strip()
                or identifier in identifiers
                or not isinstance(step, int)
                or isinstance(step, bool)
                or step < 0
                or (last_step is not None and step <= last_step)
            ):
                raise ValueError("Invalid task continuation request")
            identifiers.add(identifier)
            last_step = step
            responses.append(
                {
                    "type": "task_continue_response",
                    "continue_request_id": identifier,
                }
            )
        return responses, last_step

    @staticmethod
    def _interleave_control_responses(
        output: list[dict[str, Any]],
        responses: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        responses_by_request = {
            response.get("approval_request_id") or response.get("continue_request_id"): response
            for response in responses
        }
        continuation: list[dict[str, Any]] = []
        for item in output:
            continuation.append(item)
            if item.get("type") in {"mcp_approval_request", "task_continue_request"}:
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
        *,
        tool_names: frozenset[str] | None = None,
    ) -> tuple[AssistantEvidenceProject, ...]:
        projects: OrderedDict[int, AssistantEvidenceProject] = OrderedDict()
        for tool_name, output in cls._tool_outputs(output_items):
            if tool_name not in (tool_names or cls._READ_TOOLS):
                continue
            for payload in cls._decoded_tool_payloads(output):
                candidates = payload.get("projects")
                if isinstance(candidates, list):
                    project_values = candidates
                elif "repo_id" in payload and "evidence" in payload:
                    project_values = [payload]
                else:
                    continue
                for value in project_values:
                    project = cls._project_from_tool_value(value)
                    if project is not None and project.repo_id not in projects:
                        projects[project.repo_id] = project
                    if len(projects) >= cls._MAX_EVIDENCE_PROJECTS:
                        break
            if len(projects) >= cls._MAX_EVIDENCE_PROJECTS:
                break

        return tuple(projects.values())

    @classmethod
    def _select_visible_projects(
        cls,
        answer: str,
        history_items: list[dict[str, Any]],
        current_items: list[dict[str, Any]],
    ) -> tuple[AssistantEvidenceProject, ...]:
        current_tools = cls._called_tool_names(current_items)
        if current_tools & cls._WRITE_TOOLS:
            return ()

        detail_projects = cls._extract_evidence(
            current_items,
            tool_names=frozenset({"get_project_details"}),
        )
        if detail_projects:
            referenced, reference_detected = cls._current_projects_referenced_by_answer(
                answer,
                detail_projects,
            )
            if reference_detected:
                return referenced
            if len(detail_projects) == 1:
                return detail_projects
            return detail_projects

        current_projects = cls._extract_evidence(
            current_items,
            tool_names=frozenset({"search_projects"}),
        )
        if current_projects:
            referenced, reference_detected = cls._current_projects_referenced_by_answer(
                answer,
                current_projects,
            )
            if reference_detected:
                return referenced
            return current_projects

        history_projects = cls._extract_evidence(history_items)
        if not history_projects:
            return ()
        return cls._evidence_referenced_by_answer(answer, history_projects)

    @classmethod
    def _current_projects_referenced_by_answer(
        cls,
        answer: str,
        evidence: tuple[AssistantEvidenceProject, ...],
    ) -> tuple[tuple[AssistantEvidenceProject, ...], bool]:
        selected_ids = {
            project.repo_id for project in cls._evidence_referenced_by_answer(answer, evidence)
        }
        reference_detected = bool(selected_ids)
        projects_by_name: dict[str, list[AssistantEvidenceProject]] = {}
        for project in evidence:
            projects_by_name.setdefault(project.name.casefold(), []).append(project)

        normalized_answer = answer.casefold().replace("**", "").replace("`", "")
        for name, matching_projects in projects_by_name.items():
            if not cls._contains_exact_identity(normalized_answer, name):
                continue
            reference_detected = True
            if len(matching_projects) == 1:
                selected_ids.add(matching_projects[0].repo_id)

        return (
            tuple(project for project in evidence if project.repo_id in selected_ids),
            reference_detected,
        )

    @staticmethod
    def _contains_exact_identity(text: str, identity: str) -> bool:
        return (
            re.search(
                r"(?<![\w.-])" + re.escape(identity) + r"(?![\w.-])",
                text,
            )
            is not None
        )

    @classmethod
    def _normalize_visible_answer(
        cls,
        answer: str,
        evidence: tuple[AssistantEvidenceProject, ...],
        presentation: AssistantPresentation = AssistantPresentation.CARDS,
    ) -> str:
        if presentation is AssistantPresentation.TEXT or not evidence:
            return answer
        if presentation is AssistantPresentation.REFERENCES:
            return cls._sanitize_reference_answer(answer, evidence)
        sanitized = cls._sanitize_visible_answer(answer, evidence)
        if not (
            (len(evidence) > 1 and cls._is_duplicative_project_prose(sanitized, evidence))
            or cls._contains_raw_evidence_catalogue(sanitized)
        ):
            return sanitized
        return (
            cls._SINGLE_PROJECT_FALLBACK if len(evidence) == 1 else cls._MULTIPLE_PROJECT_FALLBACK
        )

    @classmethod
    def _select_presentation(
        cls,
        user_message: str,
        evidence: tuple[AssistantEvidenceProject, ...],
        current_items: list[dict[str, Any]],
    ) -> AssistantPresentation:
        current_tools = cls._called_tool_names(current_items)
        if current_tools & cls._WRITE_TOOLS or cls._WRITE_INTENT_PATTERN.search(user_message):
            return AssistantPresentation.TEXT
        if not evidence:
            return AssistantPresentation.TEXT
        if cls._EVIDENCE_INTENT_PATTERN.search(user_message):
            return AssistantPresentation.CARDS
        if (
            cls._COMPARISON_DETAIL_INTENT_PATTERN.search(user_message)
            or "get_project_details" in current_tools
        ):
            return AssistantPresentation.REFERENCES
        if (
            cls._SEARCH_LIST_INTENT_PATTERN.search(user_message)
            or "search_projects" in current_tools
        ):
            return AssistantPresentation.CARDS
        return AssistantPresentation.REFERENCES

    @staticmethod
    def _latest_user_message(input_items: list[dict[str, Any]]) -> str:
        for item in reversed(input_items):
            if item.get("role") != "user":
                continue
            content = item.get("content")
            if isinstance(content, str):
                return content
        return ""

    @classmethod
    def _sanitize_reference_answer(
        cls,
        answer: str,
        evidence: tuple[AssistantEvidenceProject, ...],
    ) -> str:
        urls = {project.html_url.casefold(): project for project in evidence}

        def replace_markdown_link(match: re.Match[str]) -> str:
            label, url = match.groups()
            return label if url.casefold() in urls else match.group(0)

        candidate = cls._MARKDOWN_GITHUB_LINK_PATTERN.sub(replace_markdown_link, answer)
        for project in evidence:
            candidate = re.sub(
                re.escape(project.html_url),
                project.full_name,
                candidate,
                flags=re.IGNORECASE,
            )
        candidate = cls._PARENTHETICAL_REPOSITORY_ID_PATTERN.sub("", candidate)
        candidate = "\n".join(line.rstrip() for line in candidate.splitlines()).strip()
        if not candidate or cls._EMPTY_LIST_LINE_PATTERN.search(candidate) or "()" in candidate:
            return answer
        return candidate

    @classmethod
    def _sanitize_visible_answer(
        cls,
        answer: str,
        evidence: tuple[AssistantEvidenceProject, ...],
    ) -> str:
        sanitized = cls._MARKDOWN_GITHUB_LINK_PATTERN.sub(r"\1", answer)
        for project in evidence:
            sanitized = re.sub(
                re.escape(project.html_url),
                project.full_name,
                sanitized,
                flags=re.IGNORECASE,
            )
        sanitized = cls._GITHUB_URL_PATTERN.sub("", sanitized)
        sanitized = cls._REPOSITORY_ID_CONTEXT_PATTERN.sub("", sanitized)
        sanitized = cls._REPOSITORY_ID_PATTERN.sub("", sanitized)
        sanitized = re.sub(r"\(\s*\)", "", sanitized)
        sanitized = re.sub(r"[ \t]+([,.;:!?])", r"\1", sanitized)
        sanitized = re.sub(r"[ \t]{2,}", " ", sanitized)
        return "\n".join(line.rstrip() for line in sanitized.splitlines()).strip()

    @classmethod
    def _is_duplicative_project_prose(
        cls,
        answer: str,
        evidence: tuple[AssistantEvidenceProject, ...],
    ) -> bool:
        metadata_mentions = len(cls._NUMERIC_METADATA_PATTERN.findall(answer))
        identity_occurrences = cls._repository_identity_occurrences(answer, evidence)
        if cls._has_reconstructed_metadata_list(answer, evidence):
            return True

        long_by_characters = len(answer) > cls._PROSE_CHARACTER_GUIDELINE
        long_by_words = len(answer.split()) > cls._PROSE_WORD_GUIDELINE
        supporting_signals = sum(
            (
                metadata_mentions >= 1,
                identity_occurrences >= 1,
                long_by_characters,
                long_by_words,
            )
        )
        repeated_catalogue = identity_occurrences >= 3 and (
            metadata_mentions >= 2 or long_by_characters or long_by_words
        )
        long_metadata_restatement = (
            metadata_mentions >= 1 and identity_occurrences >= 1 and supporting_signals >= 3
        )
        return repeated_catalogue or long_metadata_restatement

    @classmethod
    def _contains_raw_evidence_catalogue(cls, answer: str) -> bool:
        return (
            cls._RAW_SIMILARITY_PATTERN.search(answer) is not None
            or len(cls._RAW_CHUNK_REFERENCE_PATTERN.findall(answer)) >= 2
        )

    @classmethod
    def _has_reconstructed_metadata_list(
        cls,
        answer: str,
        evidence: tuple[AssistantEvidenceProject, ...],
    ) -> bool:
        matching_lines = 0
        for line in answer.splitlines():
            if not cls._LIST_LINE_PATTERN.match(line):
                continue
            if cls._NUMERIC_METADATA_PATTERN.search(line) is None:
                continue
            if cls._repository_identity_occurrences(line, evidence) == 0:
                continue
            matching_lines += 1
            if matching_lines >= 2:
                return True
        return False

    @classmethod
    def _repository_identity_occurrences(
        cls,
        text: str,
        evidence: tuple[AssistantEvidenceProject, ...],
    ) -> int:
        normalized = text.casefold().replace("**", "").replace("`", "")
        names: dict[str, int] = {}
        for project in evidence:
            names[project.name.casefold()] = names.get(project.name.casefold(), 0) + 1

        occurrences = 0
        for project in evidence:
            full_name = project.full_name.casefold()
            full_name_matches = len(
                re.findall(
                    r"(?<![\w.-])" + re.escape(full_name) + r"(?![\w.-])",
                    normalized,
                )
            )
            if full_name_matches:
                occurrences += full_name_matches
                continue

            name = project.name.casefold()
            if names[name] != 1:
                continue
            occurrences += len(
                re.findall(
                    r"(?<![\w.-])" + re.escape(name) + r"(?![\w.-])",
                    normalized,
                )
            )
        return occurrences

    @staticmethod
    def _evidence_referenced_by_answer(
        answer: str,
        evidence: tuple[AssistantEvidenceProject, ...],
    ) -> tuple[AssistantEvidenceProject, ...]:
        normalized_answer = answer.casefold().replace("**", "").replace("`", "")
        return tuple(
            project
            for project in evidence
            if re.search(
                r"(?<![\w.-])"
                + re.escape(project.full_name.casefold())
                + r"(?=$|[\s,;:!?)}\]]|\.(?:\s|$))",
                normalized_answer,
            )
            is not None
            or project.html_url.casefold() in normalized_answer
        )

    @classmethod
    def _called_tool_names(cls, output_items: list[dict[str, Any]]) -> set[str]:
        names: set[str] = set()
        for item in output_items:
            if item.get("type") not in {"mcp_approval_request", "mcp_call"}:
                continue
            name = item.get("name")
            if isinstance(name, str) and name in cls._ALLOWED_MCP_TOOLS:
                names.add(name)
        return names

    @classmethod
    def _tool_outputs(
        cls,
        output_items: list[dict[str, Any]],
    ) -> list[tuple[str, Any]]:
        names_by_id: dict[str, str] = {}
        for item in output_items:
            if item.get("type") != "mcp_approval_request":
                continue
            identifier = item.get("id")
            name = item.get("name")
            if (
                isinstance(identifier, str)
                and identifier
                and isinstance(name, str)
                and name in cls._ALLOWED_MCP_TOOLS
            ):
                names_by_id[identifier] = name

        outputs: list[tuple[str, Any]] = []
        for item in output_items:
            item_type = item.get("type")
            name: Any = None
            if item_type == "mcp_call":
                name = item.get("name")
            elif item_type == "function_call_output":
                call_id = item.get("call_id")
                if isinstance(call_id, str):
                    name = names_by_id.get(call_id)
                if name is None:
                    name = item.get("name")
            if isinstance(name, str) and name in cls._ALLOWED_MCP_TOOLS:
                outputs.append((name, item.get("output")))
        return outputs

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
    def _project_from_tool_value(cls, value: Any) -> AssistantEvidenceProject | None:
        if not isinstance(value, dict):
            return None
        repo_id = value.get("repo_id")
        name = cls._bounded_required_text(value.get("name"), 200)
        full_name = value.get("full_name")
        owner = cls._bounded_required_text(value.get("owner"), 200)
        description = cls._bounded_optional_text(
            value.get("description"), cls._MAX_DESCRIPTION_LENGTH
        )
        html_url = value.get("html_url")
        evidence = value.get("evidence")
        primary_language = cls._bounded_optional_text(value.get("primary_language"), 100)
        license_name = cls._bounded_optional_text(value.get("license"), 100)
        stars = cls._nonnegative_integer(value.get("stars"))
        forks = cls._nonnegative_integer(value.get("forks"))
        open_issues = cls._nonnegative_integer(value.get("open_issues"))
        if (
            not isinstance(repo_id, int)
            or isinstance(repo_id, bool)
            or repo_id <= 0
            or name is None
            or not isinstance(full_name, str)
            or not full_name.strip()
            or len(full_name) > 200
            or owner is None
            or not isinstance(html_url, str)
            or not cls._valid_github_repository_url(html_url, full_name)
            or not isinstance(evidence, list)
            or stars is None
            or forks is None
            or open_issues is None
        ):
            return None

        topics: list[str] = []
        raw_topics = value.get("topics")
        if isinstance(raw_topics, list):
            for topic in raw_topics:
                bounded = cls._bounded_required_text(topic, 100)
                if bounded is not None and bounded not in topics:
                    topics.append(bounded)
                if len(topics) >= cls._MAX_TOPICS:
                    break

        chunks: list[AssistantEvidenceChunk] = []
        chunk_indexes: set[int] = set()
        for chunk in evidence:
            if len(chunks) >= cls._MAX_EVIDENCE_CHUNKS:
                break
            if not isinstance(chunk, dict):
                continue
            chunk_index = chunk.get("chunk_index")
            chunk_text = chunk.get("chunk_text")
            if (
                not isinstance(chunk_index, int)
                or isinstance(chunk_index, bool)
                or chunk_index < 0
                or chunk_index in chunk_indexes
                or not isinstance(chunk_text, str)
                or not chunk_text.strip()
                or len(chunk_text) > cls._MAX_EVIDENCE_TEXT_LENGTH
            ):
                continue
            chunk_indexes.add(chunk_index)
            chunks.append(
                AssistantEvidenceChunk(
                    chunk_index=chunk_index,
                    chunk_text=chunk_text.strip(),
                    similarity=cls._finite_similarity(chunk.get("similarity")),
                )
            )

        return AssistantEvidenceProject(
            repo_id=repo_id,
            name=name,
            full_name=full_name.strip(),
            owner=owner,
            description=description,
            html_url=html_url,
            primary_language=primary_language,
            stars=stars,
            forks=forks,
            open_issues=open_issues,
            topics=tuple(topics),
            license=license_name,
            similarity=cls._finite_similarity(value.get("similarity")),
            evidence=tuple(chunks),
        )

    @staticmethod
    def _bounded_required_text(value: Any, maximum: int) -> str | None:
        if not isinstance(value, str):
            return None
        stripped = value.strip()
        return stripped if stripped and len(stripped) <= maximum else None

    @staticmethod
    def _bounded_optional_text(value: Any, maximum: int) -> str | None:
        if value is None:
            return None
        return SupervisorClient._bounded_required_text(value, maximum)

    @staticmethod
    def _nonnegative_integer(value: Any) -> int | None:
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            return None
        return value

    @staticmethod
    def _finite_similarity(value: Any) -> float | None:
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            return None
        similarity = float(value)
        if not math.isfinite(similarity) or similarity < -1.0 or similarity > 1.0:
            return None
        return similarity

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
    def _failure_message(control: SupervisorTurnControl, safe_message: str) -> str:
        return UNCERTAIN_COMPLETION_MESSAGE if control.write_may_have_executed else safe_message

    @classmethod
    def _raise_bad_gateway(cls, control: SupervisorTurnControl) -> NoReturn:
        raise SupervisorBadGatewayError(
            cls._failure_message(control, "Ask RepoScout returned an invalid response"),
            uncertain=control.write_may_have_executed,
        )

    @classmethod
    def _raise_timeout(cls, control: SupervisorTurnControl) -> NoReturn:
        raise SupervisorTimeoutError(
            cls._failure_message(control, "Ask RepoScout timed out. Please try again."),
            uncertain=control.write_may_have_executed,
        )

    @classmethod
    def _raise_http_error(
        cls,
        status_code: int,
        retry_after: int | None,
        control: SupervisorTurnControl,
    ) -> NoReturn:
        uncertain = control.write_may_have_executed
        message = cls._failure_message(control, "Ask RepoScout is temporarily unavailable")
        if status_code in {408, 504}:
            cls._raise_timeout(control)
        if status_code in {429, 502, 503}:
            raise SupervisorUnavailableError(
                message,
                retry_after=retry_after,
                uncertain=uncertain,
            )
        raise SupervisorBadGatewayError(message, uncertain=uncertain)


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
    presentation: AssistantPresentation = AssistantPresentation.TEXT


@dataclass(frozen=True, slots=True)
class AssistantCancellationResult:
    outcome: AssistantCancellationOutcome
    reply: AssistantReply | None = None


@dataclass(slots=True)
class ActiveAssistantTurn:
    control: SupervisorTurnControl
    task: asyncio.Task[AssistantReply] | None = None


@dataclass(frozen=True, slots=True)
class CompletedAssistantTurn:
    reply: AssistantReply
    completed_at: float


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
        completed_turn_ttl_seconds: float = 60.0,
        max_completed_turns: int = 100,
        clock: Callable[[], float] = monotonic,
        id_factory: Callable[[], UUID] = uuid4,
    ) -> None:
        self._client = client
        self._max_turns = max_turns
        self._ttl_seconds = ttl_seconds
        self._max_conversations = max_conversations
        self._max_history_bytes = max_history_bytes
        self._completed_turn_ttl_seconds = completed_turn_ttl_seconds
        self._max_completed_turns = max_completed_turns
        self._clock = clock
        self._id_factory = id_factory
        self._conversations: OrderedDict[UUID, ConversationState] = OrderedDict()
        self._state_lock = asyncio.Lock()
        self._turn_lock = asyncio.Lock()
        self._active_turns: dict[UUID, ActiveAssistantTurn] = {}
        self._completed_turns: OrderedDict[UUID, CompletedAssistantTurn] = OrderedDict()

    async def send(
        self,
        conversation_id: UUID | None,
        message: str,
        *,
        progress: ProgressCallback | None = None,
        control: SupervisorTurnControl | None = None,
    ) -> AssistantReply:
        identifier, state, is_new = await self._begin_turn(conversation_id)
        turn_control = control or SupervisorTurnControl()
        user_message: dict[str, Any] = {
            "type": "message",
            "role": "user",
            "content": message,
        }
        input_items = self._flatten_history(state.turns)
        input_items.append(deepcopy(user_message))

        try:
            result = await self._client.send(
                input_items,
                progress=progress,
                control=turn_control,
            )
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
            presentation=result.presentation,
        )

    async def start_turn(
        self,
        turn_id: UUID,
        conversation_id: UUID | None,
        message: str,
        *,
        progress: ProgressCallback,
    ) -> asyncio.Task[AssistantReply]:
        control = SupervisorTurnControl()
        active = ActiveAssistantTurn(control=control)

        async def execute() -> AssistantReply:
            try:
                reply = await self.send(
                    conversation_id,
                    message,
                    progress=progress,
                    control=control,
                )
            except BaseException:
                async with self._turn_lock:
                    if self._active_turns.get(turn_id) is active:
                        del self._active_turns[turn_id]
                raise
            async with self._turn_lock:
                if self._active_turns.get(turn_id) is active:
                    self._completed_turns[turn_id] = CompletedAssistantTurn(
                        reply=reply,
                        completed_at=self._clock(),
                    )
                    self._completed_turns.move_to_end(turn_id)
                    del self._active_turns[turn_id]
                    self._bound_completed_turns()
            return reply

        async with self._turn_lock:
            self._remove_expired_completed_turns(self._clock())
            if turn_id in self._active_turns or turn_id in self._completed_turns:
                raise AssistantTurnConflictError("Assistant turn already exists")
            task = asyncio.create_task(execute(), name=f"assistant-turn-{turn_id}")
            active.task = task
            self._active_turns[turn_id] = active
        return task

    async def cancel_turn(self, turn_id: UUID) -> AssistantCancellationResult:
        async with self._turn_lock:
            self._remove_expired_completed_turns(self._clock())
            completed = self._completed_turns.get(turn_id)
            if completed is not None:
                return AssistantCancellationResult(
                    outcome=AssistantCancellationOutcome.COMPLETED,
                    reply=completed.reply,
                )
            active = self._active_turns.get(turn_id)
        if active is None or active.task is None:
            return AssistantCancellationResult(AssistantCancellationOutcome.UNCERTAIN)

        write_may_have_executed = await active.control.request_cancellation()
        task = active.task
        if task.done():
            return await self._completed_or_cancelled_result(task, write_may_have_executed)
        task.cancel()
        return await self._completed_or_cancelled_result(task, write_may_have_executed)

    async def disconnect_turn(self, turn_id: UUID) -> None:
        await self.cancel_turn(turn_id)

    async def _completed_or_cancelled_result(
        self,
        task: asyncio.Task[AssistantReply],
        write_may_have_executed: bool,
    ) -> AssistantCancellationResult:
        try:
            reply = await task
        except asyncio.CancelledError:
            outcome = (
                AssistantCancellationOutcome.UNCERTAIN
                if write_may_have_executed
                else AssistantCancellationOutcome.CANCELLED
            )
            return AssistantCancellationResult(outcome)
        except Exception:
            outcome = (
                AssistantCancellationOutcome.UNCERTAIN
                if write_may_have_executed
                else AssistantCancellationOutcome.CANCELLED
            )
            return AssistantCancellationResult(outcome)
        return AssistantCancellationResult(
            outcome=AssistantCancellationOutcome.COMPLETED,
            reply=reply,
        )

    def _remove_expired_completed_turns(self, now: float) -> None:
        expired = [
            turn_id
            for turn_id, completed in self._completed_turns.items()
            if now - completed.completed_at >= self._completed_turn_ttl_seconds
        ]
        for turn_id in expired:
            del self._completed_turns[turn_id]

    def _bound_completed_turns(self) -> None:
        while len(self._completed_turns) > self._max_completed_turns:
            self._completed_turns.popitem(last=False)

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
