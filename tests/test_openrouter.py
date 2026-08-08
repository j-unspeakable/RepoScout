import json

import httpx
import pytest

from app.config import AppEnvironment, Settings
from app.services.openrouter import (
    OpenRouterBadGateway,
    OpenRouterClient,
    OpenRouterServiceUnavailable,
    OpenRouterTimeout,
)


def _settings() -> Settings:
    return Settings(app_env=AppEnvironment.TEST, llm_api_key="openrouter-secret")


def _client(handler) -> OpenRouterClient:
    transport = httpx.MockTransport(handler)
    http_client = httpx.AsyncClient(
        transport=transport,
        base_url="https://openrouter.example/api/v1",
    )
    return OpenRouterClient(_settings(), client=http_client)


@pytest.mark.asyncio
async def test_openrouter_success_uses_chat_completions_contract() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/chat/completions"
        assert request.headers["Authorization"] == "Bearer openrouter-secret"
        body = json.loads(request.content)
        assert body["model"] == "openrouter/free"
        assert body["temperature"] == 0.2
        assert body["max_tokens"] == 600
        assert body["stream"] is False
        return httpx.Response(
            200,
            json={
                "model": "provider/resolved:free",
                "choices": [
                    {
                        "message": {"role": "assistant", "content": "Grounded answer"},
                        "finish_reason": "stop",
                    }
                ],
            },
        )

    result = await _client(handler).generate([{"role": "user", "content": "query"}])

    assert result.answer == "Grounded answer"
    assert result.resolved_model == "provider/resolved:free"


@pytest.mark.asyncio
@pytest.mark.parametrize("location", ["top", "choice"])
@pytest.mark.parametrize("error_type", ["rate_limit_exceeded", "provider_overloaded"])
async def test_openrouter_maps_http_200_typed_availability_errors(
    location: str, error_type: str
) -> None:
    error = {
        "code": 429,
        "message": "secret provider detail",
        "metadata": {"error_type": error_type},
    }
    payload = {"model": "provider/model", "choices": []}
    if location == "top":
        payload["error"] = error
    else:
        payload["choices"] = [
            {
                "message": {"content": "partial"},
                "finish_reason": "error",
                "error": error,
            }
        ]

    client = _client(lambda _: httpx.Response(200, headers={"Retry-After": "12"}, json=payload))
    with pytest.raises(OpenRouterServiceUnavailable) as caught:
        await client.generate([])

    assert caught.value.retry_after == 12
    assert "secret provider detail" not in str(caught.value)


@pytest.mark.asyncio
async def test_openrouter_maps_typed_timeout_and_provider_unavailable() -> None:
    timeout_client = _client(
        lambda _: httpx.Response(
            200,
            json={
                "error": {"metadata": {"error_type": "timeout"}},
                "choices": [],
            },
        )
    )
    with pytest.raises(OpenRouterTimeout):
        await timeout_client.generate([])

    unavailable_client = _client(
        lambda _: httpx.Response(
            200,
            json={
                "model": "provider/model",
                "choices": [
                    {
                        "finish_reason": "error",
                        "message": {"content": "partial answer"},
                        "error": {"metadata": {"error_type": "provider_unavailable"}},
                    }
                ],
            },
        )
    )
    with pytest.raises(OpenRouterBadGateway):
        await unavailable_client.generate([])


@pytest.mark.asyncio
async def test_openrouter_typed_error_wins_and_numeric_in_body_code_is_fallback() -> None:
    typed_unavailable = _client(
        lambda _: httpx.Response(
            503,
            json={
                "error": {
                    "code": 503,
                    "metadata": {"error_type": "provider_unavailable"},
                }
            },
        )
    )
    with pytest.raises(OpenRouterBadGateway):
        await typed_unavailable.generate([])

    numeric_rate_limit = _client(
        lambda _: httpx.Response(200, json={"error": {"code": 429}, "choices": []})
    )
    with pytest.raises(OpenRouterServiceUnavailable):
        await numeric_rate_limit.generate([])


@pytest.mark.asyncio
async def test_openrouter_never_accepts_partial_or_malformed_completion() -> None:
    partial = _client(
        lambda _: httpx.Response(
            200,
            json={
                "model": "provider/model",
                "choices": [{"message": {"content": "partial"}, "finish_reason": "error"}],
            },
        )
    )
    with pytest.raises(OpenRouterBadGateway):
        await partial.generate([])

    malformed = _client(lambda _: httpx.Response(200, json={"choices": []}))
    with pytest.raises(OpenRouterBadGateway):
        await malformed.generate([])


@pytest.mark.asyncio
async def test_openrouter_maps_http_timeout_without_exposing_request() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("sensitive timeout", request=request)

    with pytest.raises(OpenRouterTimeout, match="Generation timed out"):
        await _client(handler).generate([])
