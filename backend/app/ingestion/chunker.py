"""Structural chunker (REQ-011).

The blocks are split into sections (the text between two headings). Each section is cut into windows of words:

- a window never crosses a section and holds at most `max_words` words, except a table row longer than the maximum,
  which is a window by itself;
- a window aims at `target_words` words; consecutive windows of a section share `round(target_words *
  overlap_percent / 100)` words;
- a table row is never split: a window starts and ends on a row boundary. When the overlap would start inside a row
  it starts at the beginning of that row, so the overlap between table windows is rounded up to whole rows; when the
  rounding would swallow the whole previous window (or the next row is longer than the maximum) there is no overlap;
- the new words of the last window of a section are at least `min_words`: a smaller remainder is merged into the
  previous window when the result fits in `max_words`, otherwise the last two windows are rebalanced.

A word is a run of non-white characters. The text between the words of a window is kept as it is in the blocks, and
blocks are separated by a blank line (two consecutive table rows by a line break). The heading text is not part of the
content: it is the section title (the path of the open headings joined by " > ").
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass

from app.ingestion.blocks import Block

_TITLE_SEPARATOR = " > "
_WORD = re.compile(r"(\s*)(\S+)")
_NO_ROW = -1


@dataclass(frozen=True)
class ChunkerConfig:
    target_words: int
    min_words: int
    max_words: int
    overlap_percent: int


@dataclass(frozen=True)
class ChunkDraft:
    position: int
    section_title: str | None
    page_start: int | None
    page_end: int | None
    content: str
    word_count: int


@dataclass(frozen=True)
class _Token:
    word: str
    separator: str  # the white space before the word, inside its window
    row: int  # index of the table row block the word belongs to, _NO_ROW for prose
    page: int | None


def chunk_blocks(blocks: Sequence[Block], config: ChunkerConfig) -> list[ChunkDraft]:
    drafts: list[ChunkDraft] = []
    for title, tokens in _sections(blocks):
        for start, end in _SectionCutter(tokens, config).windows():
            window = tokens[start:end]
            content = window[0].word + "".join(token.separator + token.word for token in window[1:])
            pages = [token.page for token in window if token.page is not None]
            drafts.append(
                ChunkDraft(
                    position=len(drafts),
                    section_title=title,
                    page_start=min(pages) if pages else None,
                    page_end=max(pages) if pages else None,
                    content=content,
                    word_count=len(window),
                )
            )
    return drafts


def _sections(blocks: Sequence[Block]) -> list[tuple[str | None, list[_Token]]]:
    """The words of every section with a body, in reading order, with the title of the section."""
    sections: list[tuple[str | None, list[_Token]]] = []
    open_headings: list[tuple[int, str]] = []
    title: str | None = None
    tokens: list[_Token] = []
    previous_kind: str | None = None
    for index, block in enumerate(blocks):
        if block.kind == "heading":
            heading = " ".join(block.text.split())
            if not heading:
                continue
            if tokens:
                sections.append((title, tokens))
                tokens = []
            level = max(block.level, 1)
            while open_headings and open_headings[-1][0] >= level:
                open_headings.pop()
            open_headings.append((level, heading))
            title = _TITLE_SEPARATOR.join(text for _, text in open_headings)
            previous_kind = None
            continue
        row = index if block.kind == "table_row" else _NO_ROW
        separator = "\n" if previous_kind == "table_row" and block.kind == "table_row" else "\n\n"
        for match in _WORD.finditer(block.text):
            tokens.append(_Token(match.group(2), separator or match.group(1), row, block.page))
            previous_kind = block.kind
            separator = ""
    if tokens:
        sections.append((title, tokens))
    return sections


class _SectionCutter:
    """Computes the windows [start, end) over the words of one section."""

    def __init__(self, tokens: Sequence[_Token], config: ChunkerConfig) -> None:
        self._rows = [token.row for token in tokens]
        self._size = len(tokens)
        self._target = min(config.target_words, config.max_words)
        self._min = config.min_words
        self._max = config.max_words
        self._overlap = round(config.target_words * config.overlap_percent / 100)
        self._row_lengths = Counter(row for row in self._rows if row != _NO_ROW)

    def windows(self) -> list[tuple[int, int]]:
        windows: list[tuple[int, int]] = []
        start = 0
        previous_end = 0
        while True:
            end = self._end(start, previous_end)
            if end is None:
                start = previous_end
                end = self._end(start, previous_end) or self._row_end(start)
            if end < self._size and end - start <= self._max and self._size - end < self._min:
                if self._size - start <= self._max:
                    end = self._size
                else:
                    rebalanced = self._rebalance(start, previous_end)
                    if rebalanced is not None:
                        windows.append((start, rebalanced[0]))
                        windows.append((rebalanced[1], self._size))
                        return windows
            windows.append((start, end))
            if end >= self._size:
                return windows
            previous_end = end
            start = self._next_start(start, end)

    def _is_boundary(self, position: int) -> bool:
        """True when a window can start or end at `position`, that is outside a table row."""
        if position <= 0 or position >= self._size:
            return True
        return self._rows[position - 1] == _NO_ROW or self._rows[position - 1] != self._rows[position]

    def _is_oversized_row_start(self, position: int) -> bool:
        if position >= self._size or self._rows[position] == _NO_ROW or not self._is_boundary(position):
            return False
        return self._row_lengths[self._rows[position]] > self._max

    def _row_end(self, start: int) -> int:
        end = start + 1
        while not self._is_boundary(end):
            end += 1
        return end

    def _end(self, start: int, previous_end: int) -> int | None:
        """End of the window that starts at `start` and holds words after `previous_end`; None if there is none."""
        limit = start + self._target
        if limit >= self._size:
            return self._size
        lowest = previous_end + 1
        for position in range(limit, lowest - 1, -1):
            if self._is_boundary(position):
                return position
        for position in range(max(limit + 1, lowest), min(start + self._max, self._size) + 1):
            if self._is_boundary(position):
                return position
        return None

    def _next_start(self, start: int, end: int) -> int:
        if end - start > self._max or self._is_oversized_row_start(end):
            return end
        position = end - self._overlap
        while position > start and not self._is_boundary(position):
            position -= 1
        return position if position > start else end

    def _rebalance(self, start: int, previous_end: int) -> tuple[int, int] | None:
        """(end of the first, start of the second) window when the words from `start` are split in two halves."""
        middle = start + (self._size - start + self._overlap + 1) // 2
        for end in range(min(middle, start + self._max), previous_end, -1):
            if self._is_boundary(end):
                second_start = self._next_start(start, end)
                if self._size - end >= self._min and self._size - second_start <= self._max:
                    return end, second_start
                return None
        return None
