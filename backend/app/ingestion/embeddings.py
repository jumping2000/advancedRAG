"""Client of the Ollama embedding endpoint (REQ-012): `POST {base_url}/api/embed`.

Errors are classified for the pipeline: connection errors, timeouts, 5xx and 429 may succeed if repeated
(`TemporaryError`); any other 4xx and a body that is not one vector per text are final (`PermanentError`). The messages
are fixed text: they never carry the texts sent or the body received. There is no retry here (T-05 adds it around the
call).
"""

from __future__ import annotations

from collections.abc import Sequence

import httpx

from app.ingestion.errors import PermanentError, TemporaryError

_TOO_MANY_REQUESTS = 429
_SERVER_ERROR = 500


class OllamaEmbedder:
    def __init__(self, base_url: str, model: str, timeout: float) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout

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
            if not isinstance(embedding, list) or not all(
                isinstance(number, int | float) and not isinstance(number, bool) for number in embedding
            ):
                raise invalid
            vectors.append([float(number) for number in embedding])
        return vectors
