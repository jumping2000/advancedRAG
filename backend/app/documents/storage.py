"""The originals of the uploaded files in UPLOAD_DIR, named by document id.

A file is first written as `{id}.part` while its size and SHA-256 are computed, then renamed to `{id}` once the
document row exists. The names come from UUIDs, never from the uploaded file name.
"""

import hashlib
import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import BinaryIO

_BLOCK_SIZE = 1024 * 1024


class FileTooLarge(Exception):
    """The source has more bytes than allowed."""


@dataclass(frozen=True)
class StoredFile:
    size: int
    sha256: str


class UploadStorage:
    def __init__(self, upload_dir: Path) -> None:
        self.upload_dir = upload_dir

    def path(self, document_id: uuid.UUID) -> Path:
        return self.upload_dir / str(document_id)

    def part_path(self, document_id: uuid.UUID) -> Path:
        return self.upload_dir / f"{document_id}.part"

    def save(self, document_id: uuid.UUID, source: BinaryIO, max_bytes: int) -> StoredFile:
        """Copy the source to `{id}.part` in blocks, computing size and SHA-256. A source with more than `max_bytes`
        bytes raises FileTooLarge after at most one byte beyond the limit was read, and leaves no file."""
        self.upload_dir.mkdir(parents=True, exist_ok=True)
        part = self.part_path(document_id)
        digest = hashlib.sha256()
        size = 0
        try:
            with part.open("xb") as target:  # "x": never overwrites an existing file
                while block := source.read(min(_BLOCK_SIZE, max_bytes + 1 - size)):
                    size += len(block)
                    if size > max_bytes:
                        raise FileTooLarge
                    digest.update(block)
                    target.write(block)
        except BaseException:
            part.unlink(missing_ok=True)
            raise
        return StoredFile(size=size, sha256=digest.hexdigest())

    def commit(self, document_id: uuid.UUID) -> None:
        """Atomic rename of `{id}.part` to `{id}`."""
        os.replace(self.part_path(document_id), self.path(document_id))

    def discard(self, document_id: uuid.UUID) -> None:
        """Remove `{id}.part`, if there is one."""
        self.part_path(document_id).unlink(missing_ok=True)

    def exists(self, document_id: uuid.UUID) -> bool:
        return self.path(document_id).is_file()

    def remove(self, document_id: uuid.UUID) -> bool:
        """Remove `{id}`; False if it was not there."""
        try:
            self.path(document_id).unlink()
        except FileNotFoundError:
            return False
        return True

    def orphans(self, older_than: timedelta, now: datetime) -> list[Path]:
        """The files of the folder, `.part` ones included, last modified more than `older_than` before `now`. It does
        not know the documents: the caller keeps those that have no document."""
        if not self.upload_dir.is_dir():
            return []
        cutoff = (now - older_than).timestamp()
        return sorted(path for path in self.upload_dir.iterdir() if path.is_file() and path.stat().st_mtime < cutoff)
