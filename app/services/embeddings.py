import asyncio
import math
from collections.abc import Callable, Sequence
from typing import Any, Protocol

from asyncer import asyncify

EMBEDDING_MODEL_NAME = "sentence-transformers/all-MiniLM-L6-v2"
EMBEDDING_DIMENSION = 384


class EmbeddingServiceError(RuntimeError):
    """A safe query-embedding failure."""


class EmbeddingServiceProtocol(Protocol):
    async def embed_query(self, query: str) -> list[float]: ...


class SentenceTransformerEmbeddingService:
    def __init__(
        self,
        model_factory: Callable[[str], Any] | None = None,
    ) -> None:
        self._model_factory = model_factory or self._default_model_factory
        self._model: Any | None = None
        self._load_lock = asyncio.Lock()
        self._encode_lock = asyncio.Lock()

    async def embed_query(self, query: str) -> list[float]:
        try:
            model = await self._get_model()
            async with self._encode_lock:
                encoded = await asyncify(model.encode)(
                    [query],
                    batch_size=1,
                    convert_to_numpy=True,
                    normalize_embeddings=True,
                    show_progress_bar=False,
                )
            vector = self._first_vector(encoded)
            self._validate_vector(vector)
            return vector
        except EmbeddingServiceError:
            raise
        except Exception as exc:
            raise EmbeddingServiceError("Unable to embed the search query") from exc

    async def _get_model(self) -> Any:
        if self._model is not None:
            return self._model
        async with self._load_lock:
            if self._model is None:
                try:
                    self._model = await asyncify(self._model_factory)(EMBEDDING_MODEL_NAME)
                except Exception as exc:
                    raise EmbeddingServiceError("Unable to load the embedding model") from exc
        return self._model

    @staticmethod
    def _default_model_factory(model_name: str) -> Any:
        from sentence_transformers import SentenceTransformer

        return SentenceTransformer(model_name)

    @staticmethod
    def _first_vector(encoded: Any) -> list[float]:
        if len(encoded) != 1:
            raise EmbeddingServiceError("Embedding model returned an unexpected batch size")
        first: Sequence[float] = encoded[0]
        return [float(value) for value in first]

    @staticmethod
    def _validate_vector(vector: list[float]) -> None:
        if len(vector) != EMBEDDING_DIMENSION:
            raise EmbeddingServiceError("Embedding model returned an invalid vector dimension")
        if not all(math.isfinite(value) for value in vector):
            raise EmbeddingServiceError("Embedding model returned a non-finite vector")
        norm = math.sqrt(sum(value * value for value in vector))
        if not math.isclose(norm, 1.0, rel_tol=1e-3, abs_tol=1e-3):
            raise EmbeddingServiceError("Embedding model returned a non-normalized vector")
