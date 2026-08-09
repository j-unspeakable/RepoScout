from typing import NoReturn

from fastapi import APIRouter, HTTPException, status

from app.dependencies import AssistantServiceDep
from app.schemas.assistant import (
    AssistantMessageRequest,
    AssistantMessageResponse,
    AssistantMessageResponseMessage,
)
from app.services.supervisor import (
    ConversationCapacityError,
    ConversationConflictError,
    ConversationExpiredError,
    SupervisorBadGatewayError,
    SupervisorError,
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
    return AssistantMessageResponse(
        conversation_id=reply.conversation_id,
        message=AssistantMessageResponseMessage(content=reply.content),
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
