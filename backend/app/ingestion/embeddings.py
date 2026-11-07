"""Client of the Ollama embedding endpoint (REQ-012)."""

from __future__ import annotations

from collections.abc import Sequence


class OllamaEmbedder:
    def __init__(self, base_url: str, model: str, timeout: float) -> None:
        self.base_url = base_url
        self.model = model
        self.timeout = timeout

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return []
