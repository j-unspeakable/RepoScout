from dataclasses import dataclass
from typing import Any

import httpx

from app.config import Settings


class OpenRouterError(RuntimeError):
    """A safe OpenRouter boundary failure."""


class OpenRouterServiceUnavailable(OpenRouterError):
    def __init__(self, message: str, retry_after: int | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class OpenRouterTimeout(OpenRouterError):
    pass


class OpenRouterBadGateway(OpenRouterError):
    pass


@dataclass(frozen=True, slots=True)
class GenerationResult:
    answer: str
    resolved_model: str


class OpenRouterClient:
    def __init__(
        self,
        settings: Settings,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if settings.llm_api_key is None:
            raise ValueError("LLM_API_KEY is required to construct OpenRouterClient")
        self.requested_model = settings.llm_model_name
        self._max_output_tokens = settings.llm_max_output_tokens
        self._owns_client = client is None
        self._headers = {
            "Authorization": f"Bearer {settings.llm_api_key.get_secret_value()}",
            "Content-Type": "application/json",
            "User-Agent": "RepoScout/0.1",
        }
        self._client = client or httpx.AsyncClient(
            base_url=settings.llm_api_base_url.rstrip("/"),
            headers=self._headers,
            timeout=httpx.Timeout(settings.llm_request_timeout, connect=10.0),
        )

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def generate(self, messages: list[dict[str, str]]) -> GenerationResult:
        try:
            response = await self._client.post(
                "/chat/completions",
                headers=self._headers,
                json={
                    "model": self.requested_model,
                    "messages": messages,
                    "temperature": 0.2,
                    "max_completion_tokens": self._max_output_tokens,
                    "stream": False,
                },
            )
        except httpx.TimeoutException as exc:
            raise OpenRouterTimeout("Generation timed out") from exc
        except httpx.RequestError as exc:
            raise OpenRouterBadGateway("Generation upstream unavailable") from exc

        retry_after = self._retry_after(response)
        try:
            payload = response.json()
        except ValueError as exc:
            self._raise_mapped_error(None, response.status_code, retry_after, exc)

        if not isinstance(payload, dict):
            self._raise_mapped_error(None, response.status_code, retry_after)

        top_level_error = payload.get("error")
        if not response.is_success or isinstance(top_level_error, dict):
            self._raise_mapped_error(
                self._error_type(top_level_error),
                self._error_status(top_level_error) or response.status_code,
                retry_after,
            )

        choices = payload.get("choices")
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            raise OpenRouterBadGateway("Generation returned a malformed response")

        choice = choices[0]
        choice_error = choice.get("error")
        if choice.get("finish_reason") == "error" or isinstance(choice_error, dict):
            self._raise_mapped_error(
                self._error_type(choice_error),
                self._error_status(choice_error) or response.status_code,
                retry_after,
            )

        if choice.get("finish_reason") != "stop":
            raise OpenRouterBadGateway("Generation returned an incomplete response")

        message = choice.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        resolved_model = payload.get("model")
        if not isinstance(content, str) or not content.strip():
            raise OpenRouterBadGateway("Generation returned an empty response")
        if not isinstance(resolved_model, str) or not resolved_model.strip():
            raise OpenRouterBadGateway("Generation returned a malformed response")

        return GenerationResult(answer=content.strip(), resolved_model=resolved_model)

    @classmethod
    def _raise_mapped_error(
        cls,
        error_type: str | None,
        status_code: int,
        retry_after: int | None,
        cause: Exception | None = None,
    ) -> None:
        if error_type is not None:
            if error_type in {"rate_limit_exceeded", "provider_overloaded"}:
                raise OpenRouterServiceUnavailable(
                    "Generation service unavailable", retry_after
                ) from cause
            if error_type == "timeout":
                raise OpenRouterTimeout("Generation timed out") from cause
            raise OpenRouterBadGateway("Generation upstream failed") from cause
        if status_code in {429, 503}:
            raise OpenRouterServiceUnavailable(
                "Generation service unavailable", retry_after
            ) from cause
        if status_code in {408, 504}:
            raise OpenRouterTimeout("Generation timed out") from cause
        raise OpenRouterBadGateway("Generation upstream failed") from cause

    @staticmethod
    def _error_type(error: Any) -> str | None:
        if not isinstance(error, dict):
            return None
        metadata = error.get("metadata")
        if isinstance(metadata, dict) and isinstance(metadata.get("error_type"), str):
            return metadata["error_type"]
        value = error.get("error_type")
        return value if isinstance(value, str) else None

    @staticmethod
    def _error_status(error: Any) -> int | None:
        if not isinstance(error, dict):
            return None
        value = error.get("code")
        return value if isinstance(value, int) else None

    @staticmethod
    def _retry_after(response: httpx.Response) -> int | None:
        value = response.headers.get("Retry-After")
        return int(value) if value and value.isdigit() and int(value) > 0 else None
