"""Client of an OpenAI-compatible chat API in streaming mode (phase 3, T-01). SKELETON: the behaviour comes next."""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from typing import Literal, TypedDict

ErrorCode = Literal["generation_error", "generation_timeout"]


class ChatMessage(TypedDict):
    role: str
    content: str


class GenerationError(Exception):
    def __init__(self, code: ErrorCode, message: str | None = None) -> None:
        super().__init__(message or "The generation failed")
        self.code: ErrorCode = code


class OpenAICompatibleClient:
    def __init__(self, base_url: str, model: str, api_key: str | None, timeout: float) -> None:
        self.base_url = base_url
        self.model = model
        self.api_key = api_key
        self.timeout = timeout

    async def stream(self, messages: Sequence[ChatMessage]) -> AsyncIterator[str]:
        return
        yield ""

    async def check(self, timeout: float) -> Literal["ok", "unreachable"]:  # noqa: ASYNC109 (the plan's interface)
        return "unreachable"
