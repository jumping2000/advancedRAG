"""Reranking of the first candidates of the fusion (SPEC-2026-11-20-fase-4-rerank-rewrite, REQ-008, REQ-009).

Skeleton of T-03: the interfaces of the plan, without the behaviour (the tests are written first).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Literal

from app.chat.retrieval import Candidate

RerankReason = Literal["unreachable", "http_error", "timeout", "invalid_response"]
RerankApi = Literal["tei", "cohere"]


class RerankError(Exception):
    """The reranking failed: `reason` tells why (the context keeps the RRF order)."""

    def __init__(self, reason: RerankReason) -> None:
        super().__init__(reason)
        self.reason: RerankReason = reason


class RerankClient:
    def __init__(self, api: RerankApi, base_url: str, model: str, api_key: str | None, timeout: float) -> None:
        self.api = api
        self.base_url = base_url
        self.model = model
        self.timeout = timeout

    async def scores(self, query: str, texts: Sequence[str]) -> list[float]:
        raise RerankError("unreachable")


def rerank_order(candidates: Sequence[Candidate], scores: Sequence[float]) -> list[Candidate]:
    return list(candidates)
