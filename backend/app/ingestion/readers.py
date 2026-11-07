"""Readers for the formats that are read directly: TXT and MD (REQ-008).

The text is UTF-8 (a leading BOM is dropped); `\\r\\n` and `\\r` become `\\n`. The result is a list of blocks in reading
order and it is not normalized: `normalize_blocks` comes next. Line-based parsing only, no regular expression is run on
whole lines, so a very long line without spaces costs linear time.
"""

from __future__ import annotations

from typing import Literal

from app.ingestion.blocks import Block

_FENCE_CHARACTERS = "`~"
_MAX_HEADING_LEVEL = 6
_MAX_FENCE_INDENT = 3
_FRONT_MATTER_MAX_LINES = 200


def read_text_document(data: bytes, fmt: Literal["txt", "md"]) -> list[Block]:
    text = data.decode("utf-8-sig").replace("\r\n", "\n").replace("\r", "\n")
    lines = text.split("\n")
    if fmt == "txt":
        return _read_paragraphs(lines)
    return _read_markdown(lines)


def _read_paragraphs(lines: list[str]) -> list[Block]:
    """Paragraphs separated by blank lines; nothing else is interpreted."""
    blocks: list[Block] = []
    current: list[str] = []
    for line in lines:
        if line.strip():
            current.append(line)
        else:
            _flush(blocks, current)
    _flush(blocks, current)
    return blocks


def _flush(blocks: list[Block], current: list[str]) -> None:
    text = "\n".join(current).strip()
    current.clear()
    if text:
        blocks.append(Block("paragraph", text))


def _read_markdown(lines: list[str]) -> list[Block]:
    blocks: list[Block] = []
    current: list[str] = []
    index = _front_matter_end(lines)
    table_rows = 0  # rows of the table that is being read, 0 outside a table
    if index:
        blocks.append(Block("paragraph", "\n".join(lines[:index])))
    while index < len(lines):
        line = lines[index]
        fence = _fence_opening(line)
        if fence is not None:
            _flush(blocks, current)
            table_rows = 0
            end = _fence_end(lines, index, fence)
            blocks.append(Block("paragraph", "\n".join(lines[index:end])))
            index = end
            continue
        index += 1
        if not line.strip():
            _flush(blocks, current)
            table_rows = 0
            continue
        heading = _parse_heading(line)
        if heading is not None:
            _flush(blocks, current)
            table_rows = 0
            level, title = heading
            if title:
                blocks.append(Block("heading", title, level=level))
            continue
        if line.lstrip().startswith("|"):
            _flush(blocks, current)
            cells = _split_cells(line)
            if (table_rows != 1 or not _is_delimiter_row(cells)) and any(cells):
                blocks.append(Block("table_row", " | ".join(cells)))
            table_rows += 1
            continue
        table_rows = 0
        current.append(line)
    _flush(blocks, current)
    return blocks


def _front_matter_end(lines: list[str]) -> int:
    """Number of lines of a YAML front matter at the very top, 0 when there is none.

    It opens with `---` on the first line and closes with `---` or `...` within the first 200 lines; every line in
    between must look like YAML (see `_looks_like_yaml`), otherwise the text is ordinary Markdown: a leading rule
    followed by another one later must not swallow the headings in between.
    """
    if not lines or lines[0].rstrip() != "---":
        return 0
    for index in range(1, min(len(lines), _FRONT_MATTER_MAX_LINES)):
        line = lines[index].rstrip()
        if line in ("---", "..."):
            return index + 1
        if not _looks_like_yaml(line):
            return 0
    return 0


def _looks_like_yaml(line: str) -> bool:
    """A blank, comment, indented, list or `key: value` line (the line has no trailing white space)."""
    if not line or line[0] in " \t#" or line == "-" or line.startswith("- "):
        return True
    key, colon, rest = line.partition(":")
    return bool(colon) and bool(key.strip()) and (not rest or rest[0] in " \t")


def _fence_opening(line: str) -> tuple[str, int] | None:
    """The fence character and length when the line opens a fenced code block."""
    stripped = line.lstrip(" ")
    if len(line) - len(stripped) > _MAX_FENCE_INDENT or not stripped or stripped[0] not in _FENCE_CHARACTERS:
        return None
    character = stripped[0]
    length = len(stripped) - len(stripped.lstrip(character))
    if length < 3:
        return None
    if character == "`" and "`" in stripped[length:]:
        return None
    return character, length


def _fence_end(lines: list[str], start: int, fence: tuple[str, int]) -> int:
    """Index after the closing fence of the block that opens at `start` (the end of the text when it never closes)."""
    character, length = fence
    for index in range(start + 1, len(lines)):
        stripped = lines[index].strip()
        if len(stripped) >= length and not stripped.strip(character):
            return index + 1
    return len(lines)


def _parse_heading(line: str) -> tuple[int, str] | None:
    """(level, title) of an ATX heading; an empty title means a heading without text."""
    stripped = line.lstrip(" ")
    if len(line) - len(stripped) > _MAX_FENCE_INDENT:
        return None
    level = 0
    while level <= _MAX_HEADING_LEVEL and level < len(stripped) and stripped[level] == "#":
        level += 1
    if level == 0 or level > _MAX_HEADING_LEVEL:
        return None
    rest = stripped[level:]
    if rest and rest[0] not in " \t":
        return None
    title = rest.strip()
    if title.endswith("#"):
        without_closing = title.rstrip("#")
        if not without_closing:
            title = ""
        elif without_closing[-1] in " \t":
            title = without_closing.rstrip()
    return level, title


def _split_cells(line: str) -> list[str]:
    """The cells of a `| a | b |` line."""
    inner = line.strip()[1:]
    if inner.endswith("|"):
        inner = inner[:-1]
    return [cell.strip() for cell in inner.split("|")]


def _is_delimiter_row(cells: list[str]) -> bool:
    """True for the row under the header of a table: cells of hyphens with optional colons (`---`, `:--`, `:-:`)."""
    return all(_is_delimiter_cell(cell) for cell in cells)


def _is_delimiter_cell(cell: str) -> bool:
    core = cell.removeprefix(":").removesuffix(":")
    return bool(core) and not core.strip("-")
