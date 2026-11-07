"""The entry point of the RQ jobs: a plain function that runs the async pipeline (REQ-008)."""

from __future__ import annotations

import asyncio
import uuid

from rq.timeouts import JobTimeoutException

from app.config import get_settings
from app.ingestion.pipeline import fail_after_timeout, process_document


def process_document_job(document_id: str) -> None:
    """What the worker runs for each job (`app.documents.queue.PROCESS_DOCUMENT_JOB`). The job owns an event loop, which
    is why the worker runs it without a fork per job (`rq.SimpleWorker`).

    RQ stops a job that exceeds its timeout with a signal whose handler raises JobTimeoutException in the main thread.
    If the event loop is idle at that moment, the exception comes out of `asyncio.run` (the pipeline's own branch only
    catches it when it lands in the pipeline's code): the document is then marked FAILED here, so that it does not stay
    PROCESSING until the next start of the worker."""
    settings = get_settings()
    parsed_id = uuid.UUID(document_id)
    try:
        asyncio.run(process_document(parsed_id, settings))
    except JobTimeoutException:
        asyncio.run(fail_after_timeout(parsed_id, settings))
