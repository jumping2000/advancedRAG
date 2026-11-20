"""Rewriting of a follow-up question into a standalone one (SPEC-2026-11-20-fase-4-rerank-rewrite). Skeleton."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Literal

from app.chat.generation import ChatMessage
from app.db.models import Message

RewriteReason = Literal["unreachable", "http_error", "timeout", "invalid_response"]

REWRITE_INSTRUCTIONS = ""


class RewriteError(Exception):
    def __init__(self, reason: RewriteReason) -> None:
        super().__init__(reason)
        self.reason: RewriteReason = reason


def build_rewrite_messages(question: str, history: Sequence[Message]) -> list[ChatMessage]:
    return []


class QuestionRewriter:
    def __init__(self, base_url: str, model: str, api_key: str | None, timeout: float, *, max_chars: int) -> None:
        self.base_url = base_url
        self.model = model
        self.timeout = timeout
        self.max_chars = max_chars

    async def rewrite(self, question: str, history: Sequence[Message]) -> str:
        return question
