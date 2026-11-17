"""Context and instructions for the model, and the cleaning of the citations (skeleton: T-03 fills it in)."""

from __future__ import annotations

from collections.abc import Sequence

from app.chat.generation import ChatMessage
from app.chat.retrieval import Candidate
from app.db.models import Message

SYSTEM_INSTRUCTIONS = ""


def build_messages(question: str, candidates: Sequence[Candidate], history: Sequence[Message]) -> list[ChatMessage]:
    raise NotImplementedError


def clean_citations(text: str, count: int) -> tuple[str, list[int]]:
    raise NotImplementedError
