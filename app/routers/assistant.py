import asyncio
from collections.abc import AsyncIterator
from typing import NoReturn
from uuid import UUID

from fastapi import APIRouter, HTTPException, status
from fastapi.sse import EventSourceResponse, ServerSentEvent

from app.dependencies import AssistantServiceDep
from app.schemas.assistant import (
    AssistantMessageRequest,
    AssistantMessageResponse,
    AssistantMessageResponseMessage,
    AssistantProgressEvent,
    AssistantStreamErrorEvent,
    AssistantStreamRequest,
    AssistantTurnCancellationResponse,
)
from app.services.supervisor import (
    UNCERTAIN_COMPLETION_MESSAGE,
    AssistantReply,
    AssistantTurnConflictError,
    ConversationCapacityError,
    ConversationConflictError,
    ConversationExpiredError,
    SupervisorBadGatewayError,
    SupervisorError,
    SupervisorProgressPhase,
    SupervisorTimeoutError,
    SupervisorUnavailableError,
)

router = APIRouter(prefix="/assistant", tags=["assistant"])


@router.post("/messages")
async def send_assistant_message(
    request: AssistantMessageRequest,
    service: AssistantServiceDep,
) -> AssistantMessageResponse:
    try:
        reply = await service.send(request.conversation_id, request.message)
    except ConversationExpiredError as exc:
        raise HTTPException(status_code=status.HTTP_410_GONE, detail=str(exc)) from exc
    except ConversationConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except ConversationCapacityError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc
    except SupervisorTimeoutError as exc:
        _raise_supervisor_error(exc, status.HTTP_504_GATEWAY_TIMEOUT)
    except SupervisorUnavailableError as exc:
        headers = {"Retry-After": str(exc.retry_after)} if exc.retry_after else None
        _raise_supervisor_error(exc, status.HTTP_503_SERVICE_UNAVAILABLE, headers)
    except SupervisorBadGatewayError as exc:
        _raise_supervisor_error(exc, status.HTTP_502_BAD_GATEWAY)
    return _message_response(reply)


@router.post("/messages/stream", response_class=EventSourceResponse)
async def stream_assistant_message(
    request: AssistantStreamRequest,
    service: AssistantServiceDep,
) -> AsyncIterator[ServerSentEvent]:
    progress_queue: asyncio.Queue[AssistantProgressEvent] = asyncio.Queue(maxsize=8)

    async def progress(phase: SupervisorProgressPhase) -> None:
        await progress_queue.put(AssistantProgressEvent(phase=phase.value))

    try:
        task = await service.start_turn(
            request.turn_id,
            request.conversation_id,
            request.message,
            progress=progress,
        )
    except AssistantTurnConflictError:
        yield ServerSentEvent(
            event="error",
            data=AssistantStreamErrorEvent(
                status=status.HTTP_409_CONFLICT,
                detail="Assistant turn already exists",
            ),
        )
        return

    progress_task: asyncio.Task[AssistantProgressEvent] | None = None
    try:
        while True:
            progress_task = asyncio.create_task(progress_queue.get())
            done, _pending = await asyncio.wait(
                {task, progress_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if progress_task in done:
                yield ServerSentEvent(event="progress", data=progress_task.result())
                progress_task = None
            if task in done:
                if progress_task is not None:
                    progress_task.cancel()
                    await asyncio.gather(progress_task, return_exceptions=True)
                    progress_task = None
                try:
                    reply = task.result()
                except Exception as exc:
                    yield ServerSentEvent(event="error", data=_stream_error(exc))
                else:
                    yield ServerSentEvent(event="result", data=_message_response(reply))
                return
    except asyncio.CancelledError:
        raise
    finally:
        if progress_task is not None:
            progress_task.cancel()
            await asyncio.gather(progress_task, return_exceptions=True)
        if not task.done():
            await service.disconnect_turn(request.turn_id)


@router.post("/turns/{turn_id}/cancel")
async def cancel_assistant_turn(
    turn_id: UUID,
    service: AssistantServiceDep,
) -> AssistantTurnCancellationResponse:
    cancellation = await service.cancel_turn(turn_id)
    return AssistantTurnCancellationResponse(
        outcome=cancellation.outcome.value,
        result=_message_response(cancellation.reply) if cancellation.reply is not None else None,
    )


def _message_response(reply: AssistantReply) -> AssistantMessageResponse:
    return AssistantMessageResponse(
        conversation_id=reply.conversation_id,
        message=AssistantMessageResponseMessage(
            content=reply.content,
            presentation=reply.presentation.value,
            evidence=list(reply.evidence),
        ),
    )


def _stream_error(exc: Exception) -> AssistantStreamErrorEvent:
    if isinstance(exc, ConversationExpiredError):
        return AssistantStreamErrorEvent(status=status.HTTP_410_GONE, detail=str(exc))
    if isinstance(exc, ConversationConflictError):
        return AssistantStreamErrorEvent(status=status.HTTP_409_CONFLICT, detail=str(exc))
    if isinstance(exc, ConversationCapacityError):
        return AssistantStreamErrorEvent(
            status=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        )
    if isinstance(exc, SupervisorTimeoutError):
        return AssistantStreamErrorEvent(
            status=status.HTTP_504_GATEWAY_TIMEOUT,
            detail=str(exc),
            uncertain=exc.uncertain,
        )
    if isinstance(exc, SupervisorUnavailableError):
        return AssistantStreamErrorEvent(
            status=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
            uncertain=exc.uncertain,
            retry_after=exc.retry_after,
        )
    if isinstance(exc, SupervisorError):
        return AssistantStreamErrorEvent(
            status=status.HTTP_502_BAD_GATEWAY,
            detail=str(exc),
            uncertain=exc.uncertain,
        )
    return AssistantStreamErrorEvent(
        status=status.HTTP_502_BAD_GATEWAY,
        detail=UNCERTAIN_COMPLETION_MESSAGE,
        uncertain=True,
    )


def _raise_supervisor_error(
    exc: SupervisorError,
    status_code: int,
    headers: dict[str, str] | None = None,
) -> NoReturn:
    response_headers = dict(headers or {})
    if exc.uncertain:
        response_headers["X-RepoScout-Completion"] = "uncertain"
    raise HTTPException(
        status_code=status_code,
        detail=str(exc),
        headers=response_headers or None,
    ) from exc
