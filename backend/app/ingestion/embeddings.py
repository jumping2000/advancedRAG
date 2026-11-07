"""Client of the Ollama embedding endpoint (REQ-012): `POST {base_url}/api/embed`.

Errors are classified for the pipeline: connection errors, timeouts, 5xx and 429 may succeed if repeated
(`TemporaryError`); any other 4xx and a body that is not one vector per text are final (`PermanentError`). The messages
are fixed text: they never carry the texts sent or the body received. There is no retry here: the pipeline repeats the
call (`with_retries`). A vector with a value that is not finite, an empty one, or vectors of different sizes are an
invalid response; vectors that are well formed but not of the size of the indexing profile are
`embedding_dimension_mismatch` (REQ-012), so no wrong vector reaches the database.
"""

from __future__ import annotations

import math
from collections.abc import Sequence

import httpx

from app.ingestion.errors import PermanentError, TemporaryError

_TOO_MANY_REQUESTS = 429
_SERVER_ERROR = 500


class OllamaEmbedder:
    def __init__(self, base_url: str, model: str, timeout: float, dimensions: int | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout
        # The size every vector must have (the one of the indexing profile); None checks only that they agree.
        self.dimensions = dimensions

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        """One vector per text, in the order of the texts."""
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(
                    f"{self.base_url}/api/embed", json={"model": self.model, "input": list(texts)}
                )
        except httpx.TransportError:
            # Connection refused, DNS failure, timeout, connection dropped: the service is not answering.
            raise TemporaryError("embedding_unreachable", "The embedding service could not be reached") from None
        status = response.status_code
        if status == _TOO_MANY_REQUESTS or status >= _SERVER_ERROR:
            raise TemporaryError("embedding_error", f"The embedding service answered with an error ({status})")
        if status >= 400:
            raise PermanentError("embedding_error", f"The embedding service refused the request ({status})")
        vectors = self._vectors(response)
        if len(vectors) != len(texts):
            raise PermanentError(
                "embedding_invalid_response", "The embedding service returned a wrong number of vectors"
            )
        if self.dimensions is not None and any(len(vector) != self.dimensions for vector in vectors):
            raise PermanentError(
                "embedding_dimension_mismatch", "The embedding vectors do not have the size of the indexing profile"
            )
        return vectors

    @staticmethod
    def _vectors(response: httpx.Response) -> list[list[float]]:
        invalid = PermanentError("embedding_invalid_response", "The embedding service returned an invalid response")
        try:
            body = response.json()
        except ValueError:
            raise invalid from None
        embeddings = body.get("embeddings") if isinstance(body, dict) else None
        if not isinstance(embeddings, list):
            raise invalid
        vectors: list[list[float]] = []
        for embedding in embeddings:
            if not isinstance(embedding, list) or not embedding:
                raise invalid
            try:
                vector = [
                    float(number)
                    for number in embedding
                    if isinstance(number, int | float) and not isinstance(number, bool)
                ]
            except OverflowError:  # an integer too large for a float
                raise invalid from None
            if len(vector) != len(embedding) or not all(math.isfinite(number) for number in vector):
                raise invalid
            vectors.append(vector)
        if len({len(vector) for vector in vectors}) > 1:
            raise invalid
        return vectors
