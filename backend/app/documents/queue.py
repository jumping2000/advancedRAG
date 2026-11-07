"""The RQ queue of the ingestion jobs (REQ-002). The job function lives in the worker's code, not in the API."""

import uuid

from redis import Redis
from rq import Queue

INGESTION_QUEUE = "ingestion"
# The job is named by its dotted path: the API never imports the ingestion code (the worker does).
PROCESS_DOCUMENT_JOB = "app.ingestion.jobs.process_document_job"
JOB_TIMEOUT_SECONDS = 3600


def enqueue_document(redis_url: str, document_id: uuid.UUID) -> None:
    """Put the processing of the document on the `ingestion` queue; the result of the job is not kept."""
    connection = Redis.from_url(redis_url)
    try:
        Queue(INGESTION_QUEUE, connection=connection).enqueue(
            PROCESS_DOCUMENT_JOB, str(document_id), job_timeout=JOB_TIMEOUT_SECONDS, result_ttl=0
        )
    finally:
        connection.close()
