import math

import pytest

from app.services.embeddings import EmbeddingServiceError, SentenceTransformerEmbeddingService


class FakeModel:
    def __init__(self, vector: list[float]) -> None:
        self.vector = vector
        self.calls: list[tuple[list[str], dict[str, object]]] = []

    def encode(self, texts: list[str], **kwargs: object) -> list[list[float]]:
        self.calls.append((texts, kwargs))
        return [self.vector]


@pytest.fixture(autouse=True)
def direct_asyncify(monkeypatch: pytest.MonkeyPatch) -> list[object]:
    bridged: list[object] = []

    def fake_asyncify(function):
        bridged.append(function)

        async def call(*args, **kwargs):
            return function(*args, **kwargs)

        return call

    monkeypatch.setattr("app.services.embeddings.asyncify", fake_asyncify)
    return bridged


@pytest.mark.asyncio
async def test_embedding_model_is_loaded_once_and_returns_normalized_384_vector(
    direct_asyncify: list[object],
) -> None:
    vector = [1.0] + [0.0] * 383
    model = FakeModel(vector)
    model_loads: list[str] = []

    def factory(model_name: str) -> FakeModel:
        model_loads.append(model_name)
        return model

    service = SentenceTransformerEmbeddingService(factory)

    first = await service.embed_query("data pipelines")
    second = await service.embed_query("workflow orchestration")

    assert model_loads == ["sentence-transformers/all-MiniLM-L6-v2"]
    assert len(model.calls) == 2
    assert model.calls[0][1]["normalize_embeddings"] is True
    assert len(first) == 384
    assert math.isclose(math.sqrt(sum(value * value for value in first)), 1.0)
    assert second == vector
    assert direct_asyncify == [factory, model.encode, model.encode]


@pytest.mark.asyncio
async def test_embedding_service_rejects_invalid_dimension_and_nonfinite_values() -> None:
    short = SentenceTransformerEmbeddingService(lambda _: FakeModel([1.0]))
    with pytest.raises(EmbeddingServiceError, match="dimension"):
        await short.embed_query("query")

    nonfinite = SentenceTransformerEmbeddingService(lambda _: FakeModel([math.nan] + [0.0] * 383))
    with pytest.raises(EmbeddingServiceError, match="non-finite"):
        await nonfinite.embed_query("query")
