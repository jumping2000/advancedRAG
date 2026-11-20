"""Reranking of the first candidates of the fusion (SPEC-2026-11-20-fase-4-rerank-rewrite, REQ-008, REQ-009, REQ-101,
REQ-102).

`RerankClient.scores(query, texts)` posts `{base_url}/rerank` in one of two formats, chosen by `RERANK_API`:
- `tei` (Hugging Face text-embeddings-inference): `{"query", "texts", "truncate": true}` (the reranker shortens a text
  over the limit of its model instead of refusing it), answered by `[{"index", "score"}, ...]`;
- `cohere` (Cohere, Jina, Infinity and others): `{"model", "query", "documents", "top_n": len(texts)}`, answered by
  `{"results": [{"index", "relevance_score"}, ...]}`.
It gives one score per text, in the order of the texts, whatever the order of the answer (the services list the texts by
score). The key, when set, is sent as `Authorization: Bearer`. No text means no request and no score.

Every failure is a `RerankError` with a `reason` and a fixed message, never the query, a text, the body received or the
key, and with no other exception attached (neither `__cause__` nor `__context__`):
- `unreachable`: the connection cannot be made or breaks, the URL is not one, the request cannot be encoded (a key
  that is not ASCII);
- `http_error`: a status that is not 2xx; a redirect is never followed (the texts and the key go to the configured URL
  only), so it is one too;
- `timeout`: the whole request (connection, headers and body) takes more than `timeout` seconds: one deadline;
- `invalid_response`: a body that is not JSON of the shape of the format, larger than `_MAX_BODY_BYTES`, an index that
  is not an integer, out of the texts or repeated, a text without its score, or a score that is not a finite number
  (NaN, Infinity, a boolean, a string such as "0.5", null).

`rerank_order(candidates, scores)` is the final order: the first `len(scores)` candidates by score, highest first (equal
scores keep the RRF order), then the other candidates in the RRF order.
"""

from __future__ import annotations

import asyncio
import json
import math
from collections.abc import Sequence
from typing import Any, Literal

import httpx
from pydantic import SecretStr

from app.chat.retrieval import Candidate

RerankReason = Literal["unreachable", "http_error", "timeout", "invalid_response"]
RerankApi = Literal["tei", "cohere"]

_MESSAGES: dict[RerankReason, str] = {
    "unreachable": "The reranker could not be reached",
    "http_error": "The reranker answered with an error",
    "timeout": "The reranker did not answer in time",
    "invalid_response": "The reranker sent an answer that cannot be used",
}
# The scores of a few tens of texts are far below this; a larger body is not an answer of the reranker
_MAX_BODY_BYTES = 1024 * 1024


class RerankError(Exception):
    """The reranking failed: `reason` tells why (the context keeps the RRF order); fixed message."""

    def __init__(self, reason: RerankReason) -> None:
        super().__init__(_MESSAGES[reason])
        self.reason: RerankReason = reason


def _number(value: Any) -> float | None:
    """The value as a finite float when it is a JSON number (an int or a float, never a boolean); None otherwise."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    try:
        number = float(value)
    except OverflowError:  # an integer too large for a float
        return None
    return number if math.isfinite(number) else None


def _scores(items: Any, count: int, field: str) -> list[float] | None:
    """One score per text from the items `[{"index", field}, ...]` of an answer to `count` texts; None when the items
    are not exactly one valid item per text."""
    if not isinstance(items, list) or len(items) != count:
        return None
    scores: list[float | None] = [None] * count
    for item in items:
        if not isinstance(item, dict):
            return None
        index = item.get("index")
        if isinstance(index, bool) or not isinstance(index, int) or not 0 <= index < count:
            return None
        score = _number(item.get(field))
        if score is None or scores[index] is not None:  # not a number, or an index repeated
            return None
        scores[index] = score
    # every index is in range and none is repeated, so each of the `count` texts has its score
    return [score for score in scores if score is not None]


class RerankClient:
    def __init__(self, api: RerankApi, base_url: str, model: str, api_key: str | None, timeout: float) -> None:
        self.api: RerankApi = api
        self.base_url = base_url.rstrip("/")
        self.model = model
        # A SecretStr: the key does not show in the repr of the client or of its attributes
        self._api_key = SecretStr(api_key.strip()) if api_key and api_key.strip() else None
        self.timeout = timeout

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._api_key.get_secret_value()}"} if self._api_key else {}

    def _body(self, query: str, texts: list[str]) -> dict[str, Any]:
        if self.api == "cohere":
            return {"model": self.model, "query": query, "documents": texts, "top_n": len(texts)}
        return {"query": query, "texts": texts, "truncate": True}

    def _read(self, body: bytes, count: int) -> list[float] | None:
        try:
            parsed: Any = json.loads(body)
        except (ValueError, RecursionError):  # UnicodeDecodeError is a ValueError
            return None
        if self.api == "cohere":
            if not isinstance(parsed, dict):
                return None
            return _scores(parsed.get("results"), count, "relevance_score")
        return _scores(parsed, count, "score")

    async def scores(self, query: str, texts: Sequence[str]) -> list[float]:
        """One score per text, in the order of the texts; `RerankError` otherwise (see the module)."""
        if not texts:
            return []
        failure: RerankReason
        try:
            scores = await self._request(query, list(texts))
        except RerankError as error:
            failure = error.reason
        except TimeoutError:
            failure = "timeout"
        except (httpx.HTTPError, httpx.InvalidURL, httpx.StreamError, OSError, UnicodeError, ValueError):
            # UnicodeError: a key with a non-ASCII character (the header); ValueError: a URL httpx cannot use
            failure = "unreachable"
        else:
            return scores
        # Raised here, outside of the handlers above: no `__context__` links it to the original exception
        raise RerankError(failure)

    async def _request(self, query: str, texts: list[str]) -> list[float]:
        # No httpx timeout: the deadline is the only one, so that it covers the whole request, the body included
        async with asyncio.timeout(self.timeout), httpx.AsyncClient(timeout=None, follow_redirects=False) as client:
            request = client.build_request(
                "POST", f"{self.base_url}/rerank", headers=self._headers(), json=self._body(query, texts)
            )
            response = await client.send(request, stream=True)
            try:
                if not response.is_success:
                    raise RerankError("http_error")
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > _MAX_BODY_BYTES:
                        raise RerankError("invalid_response")
            finally:
                await response.aclose()
        scores = self._read(bytes(body), len(texts))
        if scores is None:
            raise RerankError("invalid_response")
        return scores


def rerank_order(candidates: Sequence[Candidate], scores: Sequence[float]) -> list[Candidate]:
    """The first `len(scores)` candidates by score, highest first (a stable sort: equal scores keep the RRF order),
    then the other candidates in the RRF order. More scores than candidates is a `ValueError`."""
    if len(scores) > len(candidates):
        raise ValueError("more scores than candidates")
    scored = sorted(zip(candidates, scores, strict=False), key=lambda pair: -pair[1])
    return [candidate for candidate, _ in scored] + list(candidates[len(scores) :])
