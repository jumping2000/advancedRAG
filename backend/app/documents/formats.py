"""Accepted document formats, file name cleaning and the check of an uploaded file (REQ-002, REQ-003).

An upload is accepted only when three things name the same format: the extension of the file name (any case), the
MIME type declared by the client (when it sends one that is not application/octet-stream) and the signature of the
content. Only the beginning of a PDF and the name list of a ZIP directory are read, never the content of the archive:
an archive is not extracted, whatever its entries declare.
"""

import codecs
import re
import unicodedata
import zipfile
from email.message import Message
from pathlib import Path
from typing import Literal

DocumentFormat = Literal["pdf", "docx", "pptx", "xlsx", "txt", "md"]

FILENAME_MAX_LENGTH = 255
FALLBACK_FILENAME = "document"
_READ_BLOCK = 1024 * 1024
_PDF_SIGNATURE = b"%PDF-"
_EXTENSIONS: dict[str, DocumentFormat] = {
    "pdf": "pdf",
    "docx": "docx",
    "pptx": "pptx",
    "xlsx": "xlsx",
    "txt": "txt",
    "md": "md",
}
_MIME_TYPES: dict[DocumentFormat, frozenset[str]] = {
    "pdf": frozenset({"application/pdf"}),
    "docx": frozenset({"application/vnd.openxmlformats-officedocument.wordprocessingml.document"}),
    "pptx": frozenset({"application/vnd.openxmlformats-officedocument.presentationml.presentation"}),
    "xlsx": frozenset({"application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"}),
    "txt": frozenset({"text/plain"}),
    "md": frozenset({"text/markdown", "text/x-markdown", "text/plain"}),
}
_ZIP_MARKERS: dict[str, str] = {"docx": "word/document.xml", "pptx": "ppt/presentation.xml", "xlsx": "xl/workbook.xml"}
_PATH_SEPARATORS = re.compile(r"[\\/]")
# Control, format (bidi overrides, zero-width), surrogate and line or paragraph separator characters.
_DROPPED_CATEGORIES = frozenset({"Cc", "Cf", "Cs", "Zl", "Zp"})


class UnsupportedType(Exception):
    """The extension, the declared type and the content do not indicate the same accepted format."""


def _cut(name: str) -> str:
    """At most 255 characters; the extension is kept so that the cut name still names the same format."""
    if len(name) <= FILENAME_MAX_LENGTH:
        return name
    stem, dot, extension = name.rpartition(".")
    if dot and 0 < len(extension) < FILENAME_MAX_LENGTH // 2:
        return stem[: FILENAME_MAX_LENGTH - len(extension) - 1].rstrip() + "." + extension
    return name[:FILENAME_MAX_LENGTH].rstrip()


def sanitize_filename(raw: str) -> str:
    """The name shown to the admin: the last path component (split on / and \\), without control or format
    characters, in Unicode NFC, trimmed, at most 255 characters. A name that is empty or made of dots only becomes
    "document". The result is for display: it is never used as a path on the disk."""
    component = _PATH_SEPARATORS.split(raw)[-1]
    kept = "".join(char for char in component if unicodedata.category(char) not in _DROPPED_CATEGORIES)
    name = _cut(unicodedata.normalize("NFC", kept).strip())
    if not name.strip("."):
        return FALLBACK_FILENAME
    return name


def _declared_mime(declared_type: str | None) -> str | None:
    """The MIME type without parameters, lower case; None if absent or the generic application/octet-stream."""
    if declared_type is None or not declared_type.strip():
        return None
    message = Message()
    message["content-type"] = declared_type
    mime = message.get_content_type()
    return None if mime == "application/octet-stream" else mime


def _is_pdf(path: Path) -> bool:
    with path.open("rb") as source:
        return source.read(len(_PDF_SIGNATURE)) == _PDF_SIGNATURE


def _is_zip_with(path: Path, marker: str) -> bool:
    """True if the file is a ZIP whose directory lists the marker. Only the directory is read; any damage to the
    archive (truncated, empty, inconsistent) means "not an archive of that format"."""
    try:
        with zipfile.ZipFile(path) as archive:
            return marker in archive.namelist()
    except (zipfile.BadZipFile, zipfile.LargeZipFile, OSError, EOFError, ValueError, RuntimeError, NotImplementedError):
        return False


def _is_utf8_text(path: Path) -> bool:
    """Valid UTF-8 (a BOM is fine) without NUL bytes, read in blocks."""
    decoder = codecs.getincrementaldecoder("utf-8")()
    try:
        with path.open("rb") as source:
            while block := source.read(_READ_BLOCK):
                if b"\x00" in block:
                    return False
                decoder.decode(block)
        decoder.decode(b"", final=True)
    except UnicodeDecodeError:
        return False
    return True


def inspect_upload(path: Path, filename: str, declared_type: str | None) -> DocumentFormat:
    """The format of the saved file, or UnsupportedType if extension, declared type and content do not agree."""
    extension = filename.rpartition(".")[2].lower() if "." in filename else ""
    fmt = _EXTENSIONS.get(extension)
    if fmt is None:
        raise UnsupportedType
    mime = _declared_mime(declared_type)
    if mime is not None and mime not in _MIME_TYPES[fmt]:
        raise UnsupportedType
    if fmt == "pdf":
        valid = _is_pdf(path)
    elif fmt in _ZIP_MARKERS:
        valid = _is_zip_with(path, _ZIP_MARKERS[fmt])
    else:
        valid = _is_utf8_text(path)
    if not valid:
        raise UnsupportedType
    return fmt
