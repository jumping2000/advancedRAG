"""The entry point of the RQ jobs: a plain function that runs the async pipeline."""

from __future__ import annotations

from app.config import get_settings  # noqa: F401


def process_document_job(document_id: str) -> None:
    return None
