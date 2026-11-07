"""The originals of the uploaded files in UPLOAD_DIR, named by document id (skeleton)."""

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import BinaryIO


class FileTooLarge(Exception):
    """The source has more bytes than allowed."""


@dataclass(frozen=True)
class StoredFile:
    size: int
    sha256: str


class UploadStorage:
    def __init__(self, upload_dir: Path) -> None:
        self.upload_dir = upload_dir

    def save(self, document_id: uuid.UUID, source: BinaryIO, max_bytes: int) -> StoredFile:
        raise NotImplementedError

    def commit(self, document_id: uuid.UUID) -> None:
        raise NotImplementedError

    def discard(self, document_id: uuid.UUID) -> None:
        raise NotImplementedError

    def path(self, document_id: uuid.UUID) -> Path:
        raise NotImplementedError

    def exists(self, document_id: uuid.UUID) -> bool:
        raise NotImplementedError

    def remove(self, document_id: uuid.UUID) -> bool:
        raise NotImplementedError

    def orphans(self, older_than: timedelta, now: datetime) -> list[Path]:
        raise NotImplementedError
