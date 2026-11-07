"""The entry point of the RQ jobs: a plain function that runs the async pipeline (REQ-008)."""

from __future__ import annotations

import asyncio
import uuid

from app.config import get_settings
from app.ingestion.pipeline import process_document


def process_document_job(document_id: str) -> None:
    """What the worker runs for each job (`app.documents.queue.PROCESS_DOCUMENT_JOB`). The job owns an event loop, which
    is why the worker runs it without a fork per job (`rq.SimpleWorker`)."""
    asyncio.run(process_document(uuid.UUID(document_id), get_settings()))
