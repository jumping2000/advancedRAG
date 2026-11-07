"""The RQ queue of the ingestion jobs (skeleton)."""

import uuid

INGESTION_QUEUE = "ingestion"


def enqueue_document(redis_url: str, document_id: uuid.UUID) -> None:
    raise NotImplementedError
