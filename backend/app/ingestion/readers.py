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
    if index:
        blocks.append(Block("paragraph", "\n".join(lines[:index])))
    while index < len(lines):
        line = lines[index]
        fence = _fence_opening(line)
        if fence is not None:
            _flush(blocks, current)
            end = _fence_end(lines, index, fence)
            blocks.append(Block("paragraph", "\n".join(lines[index:end])))
            index = end
            continue
        index += 1
        if not line.strip():
            _flush(blocks, current)
            continue
        heading = _parse_heading(line)
        if heading is not None:
            _flush(blocks, current)
            level, title = heading
            if title:
                blocks.append(Block("heading", title, level=level))
            continue
        if line.lstrip().startswith("|"):
            _flush(blocks, current)
            row = _parse_table_row(line)
            if row:
                blocks.append(Block("table_row", row))
            continue
        current.append(line)
    _flush(blocks, current)
    return blocks


def _front_matter_end(lines: list[str]) -> int:
    """Number of lines of a YAML front matter at the very top (`---` ... `---` or `...`), 0 when there is none."""
    if not lines or lines[0].rstrip() != "---":
        return 0
    for index in range(1, len(lines)):
        if lines[index].rstrip() in ("---", "..."):
            return index + 1
    return 0


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


def _parse_table_row(line: str) -> str:
    """The cells of a `| a | b |` line joined with " | "; empty for a separator row (`|---|:--:|`) or empty cells."""
    inner = line.strip()[1:]
    if inner.endswith("|"):
        inner = inner[:-1]
    cells = [cell.strip() for cell in inner.split("|")]
    if all(cell and not cell.strip("-:") and "-" in cell for cell in cells):
        return ""
    if not any(cells):
        return ""
    return " | ".join(cells)
