"""Errors of the ingestion pipeline: each one carries the reason code and message the document will show (REQ-007)."""

from __future__ import annotations

from typing import Literal

ReasonCode = Literal[
    "docling_unreachable",
    "docling_error",
    "parse_failed",
    "embedding_unreachable",
    "embedding_error",
    "embedding_invalid_response",
    "embedding_dimension_mismatch",
    "verification_failed",
    "no_text_extracted",
    "unexpected_error",
]


class IngestionError(Exception):
    """A step of the pipeline failed. `code` and `message` become the `status_reason` of the document: the message is
    fixed text for the admin, never the content or the name of a file."""

    def __init__(self, code: ReasonCode, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code: ReasonCode = code
        self.message = message


class TemporaryError(IngestionError):
    """The step may succeed if repeated: connection refused, timeout, 5xx or 429."""


class PermanentError(IngestionError):
    """Repeating the step cannot help."""
