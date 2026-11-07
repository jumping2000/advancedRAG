"""Client of docling-serve and the reader of its DoclingDocument (REQ-008, REQ-016).

`DoclingClient.convert` sends the file to `POST {base_url}/v1/convert/file` (multipart field `files`; form fields
`from_formats` = the one format, `to_formats` = json, `do_ocr` = false: there is no OCR, `include_images` = true or
false) and reads `document.json_content` of the answer. Errors are classified for the pipeline:
- may succeed if repeated (`TemporaryError`): connection errors and timeouts (`docling_unreachable`), 5xx and 429
  (`docling_error`);
- final (`PermanentError`): a refused key, 401 or 403 (`docling_error`, the message names DOCLING_API_KEY); a refusal
  of the file, any other 4xx or `status: failure` (`parse_failed`); an answer that is not a conversion
  (`docling_error`).
A `partial_success` is accepted: the number of errors Docling reported (never their text) is left in `partial_errors`
for the caller to log. The messages are fixed text: they never carry the file name, the content, or what Docling says.

`docling_json_to_blocks` walks the DoclingDocument depth first from `body.children`, whose `$ref`s name entries of
`texts`, `groups`, `tables` and `pictures` (and which have `children` of their own). The title and the section headers
are headings, every other text (text, list item, caption, code, ...) a paragraph, and a table gives one `table_row` per
row of `data.grid`, the cells joined by " | ". A table without `grid` is rebuilt from `data.table_cells` by their row
and column offsets; a merged cell repeats its text in each row it spans (a known limit). The runs of an inline group are
the pieces of one paragraph and are joined by a space. Pictures and everything of the `furniture` layer (headers and
footers) are left out. The page is the first `prov[].page_no`, None when there is none. The reader never raises on a
document with missing or wrong parts, and every node is read at most once, so references that loop end. A node nested
deeper than 200 levels is not read: the walk stops there instead of exhausting the stack.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from pathlib import Path
from typing import TypeGuard

import httpx

from app.documents.formats import DocumentFormat
from app.ingestion.blocks import Block
from app.ingestion.errors import PermanentError, TemporaryError

_CONVERT_PATH = "/v1/convert/file"
_MIME_TYPES: dict[str, str] = {
    "pdf": "application/pdf",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}
_REFUSED_KEY = (401, 403)
_TOO_MANY_REQUESTS = 429
_SERVER_ERROR = 500
_CLIENT_ERROR = 400
_MAX_DEPTH = 200
_MAX_INDEX_DIGITS = 6  # a reference to more than a million nodes is not one the reader follows
_COLLECTIONS = ("texts", "groups", "tables", "pictures")
_FURNITURE_LABELS = ("page_header", "page_footer")


class DoclingClient:
    def __init__(self, base_url: str, api_key: str | None, timeout: float, include_images: bool = False) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout
        self.include_images = include_images
        # The number of errors Docling reported on its last conversion when it answered `partial_success`, else None.
        self.partial_errors: int | None = None

    async def convert(self, path: Path, filename: str, fmt: DocumentFormat) -> list[Block]:
        """Send the file to Docling and return its blocks in reading order."""
        self.partial_errors = None
        data = await asyncio.to_thread(path.read_bytes)
        headers = {"X-Api-Key": self.api_key} if self.api_key else {}
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(
                    f"{self.base_url}{_CONVERT_PATH}",
                    headers=headers,
                    data={
                        "from_formats": fmt,
                        "to_formats": "json",
                        "do_ocr": "false",
                        "include_images": "true" if self.include_images else "false",
                    },
                    files={"files": (filename, data, _MIME_TYPES.get(fmt, "application/octet-stream"))},
                )
        except httpx.TransportError:
            # Connection refused, DNS failure, timeout, connection dropped: the service is not answering.
            raise TemporaryError("docling_unreachable", "The document parsing service could not be reached") from None
        status = response.status_code
        if status == _TOO_MANY_REQUESTS or status >= _SERVER_ERROR:
            raise TemporaryError("docling_error", f"The document parsing service answered with an error ({status})")
        if status in _REFUSED_KEY:
            raise PermanentError(
                "docling_error", "The document parsing service refused the request: check DOCLING_API_KEY"
            )
        if status >= _CLIENT_ERROR:
            raise PermanentError("parse_failed", "The document could not be parsed")
        return docling_json_to_blocks(self._json_content(response))

    def _json_content(self, response: httpx.Response) -> Mapping[str, object]:
        invalid = PermanentError("docling_error", "The document parsing service returned an invalid response")
        if response.status_code != httpx.codes.OK:
            raise invalid
        try:
            body = response.json()
        except ValueError:
            raise invalid from None
        if not isinstance(body, dict):
            raise invalid
        if body.get("status") == "failure":
            raise PermanentError("parse_failed", "The document could not be parsed")
        document = body.get("document")
        content = document.get("json_content") if isinstance(document, dict) else None
        if not isinstance(content, dict) or not content:
            raise invalid
        if body.get("status") == "partial_success":
            errors = body.get("errors")
            self.partial_errors = len(errors) if isinstance(errors, list) else 0
        return content


def docling_json_to_blocks(json_content: Mapping[str, object]) -> list[Block]:
    """The blocks of a DoclingDocument, in reading order (see the module docstring)."""
    if not isinstance(json_content, Mapping):
        return []
    return _Reader(json_content).blocks()


class _Reader:
    def __init__(self, document: Mapping[str, object]) -> None:
        self.document = document
        self.seen: set[tuple[str, int]] = set()
        self.result: list[Block] = []

    def blocks(self) -> list[Block]:
        body = self.document.get("body")
        if isinstance(body, Mapping):
            self._visit_children(body, 0)
        return self.result

    def _visit_children(self, node: Mapping[str, object], depth: int) -> None:
        children = node.get("children")
        if isinstance(children, list):
            for child in children:
                self._visit(child, depth)

    def _resolve(self, reference: object) -> tuple[tuple[str, int], Mapping[str, object]] | None:
        """The node a `{"$ref": "#/texts/3"}` names, with its key; None for anything else."""
        target = reference.get("$ref") if isinstance(reference, Mapping) else None
        parts = target.split("/") if isinstance(target, str) else []
        if len(parts) != 3 or parts[0] != "#" or parts[1] not in _COLLECTIONS or not _is_index(parts[2]):
            return None
        collection, index = parts[1], int(parts[2])
        nodes = self.document.get(collection)
        if not isinstance(nodes, list) or index >= len(nodes) or not isinstance(nodes[index], Mapping):
            return None
        return (collection, index), nodes[index]

    def _visit(self, reference: object, depth: int) -> None:
        resolved = self._resolve(reference)
        if resolved is None or depth > _MAX_DEPTH:
            return
        key, node = resolved
        if key in self.seen:
            return
        self.seen.add(key)
        if node.get("content_layer") == "furniture":
            return
        collection = key[0]
        if collection == "texts":
            self._text(node)
            self._visit_children(node, depth + 1)
        elif collection == "groups":
            if node.get("label") == "inline":
                self._inline(node, depth)
            else:
                self._visit_children(node, depth + 1)
        elif collection == "tables":
            self._table(node)
            self._visit_children(node, depth + 1)
        # pictures: nothing to read

    def _text(self, node: Mapping[str, object]) -> None:
        text = node.get("text")
        label = node.get("label")
        if not isinstance(text, str) or not text.strip() or label in _FURNITURE_LABELS:
            return
        if label == "title":
            self.result.append(Block("heading", text.strip(), level=1, page=_page(node)))
        elif label == "section_header":
            self.result.append(Block("heading", text.strip(), level=_level(node), page=_page(node)))
        else:
            self.result.append(Block("paragraph", text.strip(), page=_page(node)))

    def _inline(self, group: Mapping[str, object], depth: int) -> None:
        """The runs of one paragraph: consecutive plain texts without children are joined into one paragraph."""
        pieces: list[str] = []
        page: int | None = None

        def flush() -> None:
            nonlocal page
            if pieces:
                self.result.append(Block("paragraph", " ".join(pieces), page=page))
            pieces.clear()
            page = None

        children = group.get("children")
        for child in children if isinstance(children, list) else []:
            resolved = self._resolve(child)
            if resolved is not None and resolved[0] not in self.seen and _is_plain_run(resolved[1]):
                self.seen.add(resolved[0])
                text = resolved[1].get("text")
                if isinstance(text, str) and text.strip():
                    if not pieces:
                        page = _page(resolved[1])
                    pieces.append(text.strip())
            else:
                flush()
                self._visit(child, depth + 1)
        flush()

    def _table(self, node: Mapping[str, object]) -> None:
        data = node.get("data")
        grid = data.get("grid") if isinstance(data, Mapping) else None
        if isinstance(grid, list):
            rows = [[_cell_text(cell) for cell in row] if isinstance(row, list) else [] for row in grid]
        else:
            rows = _rows_from_cells(data.get("table_cells") if isinstance(data, Mapping) else None)
        page = _page(node)
        for cells in rows:
            if any(cells):
                self.result.append(Block("table_row", " | ".join(cells), page=page))


def _is_index(text: str) -> bool:
    """ASCII digits only and a short number: `int()` of 5000 digits raises, and `isdecimal()` accepts other scripts."""
    return 0 < len(text) <= _MAX_INDEX_DIGITS and text.isascii() and text.isdigit()


def _is_plain_run(node: Mapping[str, object]) -> bool:
    children = node.get("children")
    return (
        node.get("label") not in ("title", "section_header", *_FURNITURE_LABELS)
        and node.get("content_layer") != "furniture"
        and not (isinstance(children, list) and children)
    )


def _rows_from_cells(table_cells: object) -> list[list[str]]:
    """The rows of a table that has no `grid`, rebuilt from its cells: by start row, each row in start column order. A
    cell without integer offsets is ignored."""
    found: list[tuple[int, int, str]] = []
    for cell in table_cells if isinstance(table_cells, list) else []:
        if not isinstance(cell, Mapping):
            continue
        row, column = cell.get("start_row_offset_idx"), cell.get("start_col_offset_idx")
        if _is_int(row) and _is_int(column):
            found.append((row, column, _cell_text(cell)))
    rows: dict[int, list[tuple[int, str]]] = {}
    for row, column, text in found:
        rows.setdefault(row, []).append((column, text))
    return [[text for _, text in sorted(rows[row], key=lambda entry: entry[0])] for row in sorted(rows)]


def _is_int(value: object) -> TypeGuard[int]:
    return isinstance(value, int) and not isinstance(value, bool)


def _cell_text(cell: object) -> str:
    text = cell.get("text") if isinstance(cell, Mapping) else cell
    return text.strip() if isinstance(text, str) else ""


def _level(node: Mapping[str, object]) -> int:
    level = node.get("level")
    return level if _is_int(level) and level >= 1 else 1


def _page(node: Mapping[str, object]) -> int | None:
    """The first page number of `prov`, None when there is none."""
    prov = node.get("prov")
    for entry in prov if isinstance(prov, list) else []:
        number = entry.get("page_no") if isinstance(entry, Mapping) else None
        if isinstance(number, int) and not isinstance(number, bool) and number >= 1:
            return number
    return None
