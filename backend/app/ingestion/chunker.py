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
from array import array
from collections.abc import Iterator, Sequence
from dataclasses import dataclass

from app.ingestion.blocks import Block

_TITLE_SEPARATOR = " > "
_WORD = re.compile(r"\S+")
_NO_ROW = -1


@dataclass(frozen=True)
class ChunkerConfig:
    target_words: int
    min_words: int
    max_words: int
    overlap_percent: int

    def __post_init__(self) -> None:
        if not 0 <= self.min_words <= self.target_words <= self.max_words or self.target_words < 1:
            raise ValueError(
                "chunker words must satisfy 0 <= min_words <= target_words <= max_words and target_words >= 1"
            )
        if not 0 <= self.overlap_percent < 100:
            raise ValueError("overlap_percent must be at least 0 and below 100")


@dataclass(frozen=True)
class ChunkDraft:
    position: int
    section_title: str | None
    page_start: int | None
    page_end: int | None
    content: str
    word_count: int


class _Section:
    """The words of one section, kept as offsets into one string (a few bytes per word instead of one object)."""

    def __init__(self, title: str | None) -> None:
        self.title = title
        self.starts = array("q")  # offset of the first character of every word in `text`
        self.ends = array("q")  # offset after the last character of every word
        self.block_of = array("i")  # index (among the blocks with words of the section) of the block of every word
        self.block_pages: list[int | None] = []
        self.block_is_row: list[bool] = []
        self.block_words: list[int] = []
        self.text = ""
        self._pieces: list[str] = []
        self._length = 0

    def add_block(self, block: Block) -> None:
        words = _WORD.finditer(block.text)
        first = next(words, None)
        if first is None:
            return
        separator = ""
        if self._pieces:
            separator = "\n" if block.kind == "table_row" and self.block_is_row[-1] else "\n\n"
        piece = block.text.strip()
        shift = self._length + len(separator) - first.start()
        index = len(self.block_pages)
        self.starts.append(first.start() + shift)
        self.ends.append(first.end() + shift)
        count = 1
        for match in words:
            self.starts.append(match.start() + shift)
            self.ends.append(match.end() + shift)
            count += 1
        self.block_of.extend(array("i", [index]) * count)
        self.block_pages.append(block.page)
        self.block_is_row.append(block.kind == "table_row")
        self.block_words.append(count)
        self._pieces.append(separator + piece)
        self._length += len(separator) + len(piece)

    def close(self) -> None:
        self.text = "".join(self._pieces)
        self._pieces = []

    @property
    def size(self) -> int:
        return len(self.starts)

    def content(self, start: int, end: int) -> str:
        return self.text[self.starts[start] : self.ends[end - 1]]

    def pages(self, start: int, end: int) -> list[int]:
        blocks = self.block_pages[self.block_of[start] : self.block_of[end - 1] + 1]
        return [page for page in blocks if page is not None]


def chunk_blocks(blocks: Sequence[Block], config: ChunkerConfig) -> list[ChunkDraft]:
    drafts: list[ChunkDraft] = []
    for section in _sections(blocks):
        for start, end in _SectionCutter(section, config).windows():
            pages = section.pages(start, end)
            drafts.append(
                ChunkDraft(
                    position=len(drafts),
                    section_title=section.title,
                    page_start=min(pages) if pages else None,
                    page_end=max(pages) if pages else None,
                    content=section.content(start, end),
                    word_count=end - start,
                )
            )
    return drafts


def _sections(blocks: Sequence[Block]) -> Iterator[_Section]:
    """The sections that have a body, in reading order, one at a time."""
    open_headings: list[tuple[int, str]] = []
    section = _Section(None)
    for block in blocks:
        if block.kind != "heading":
            section.add_block(block)
            continue
        heading = " ".join(block.text.split())
        if not heading:
            continue
        if section.size:
            section.close()
            yield section
        level = max(block.level, 1)
        while open_headings and open_headings[-1][0] >= level:
            open_headings.pop()
        open_headings.append((level, heading))
        section = _Section(_TITLE_SEPARATOR.join(text for _, text in open_headings))
    if section.size:
        section.close()
        yield section


class _SectionCutter:
    """Computes the windows [start, end) over the words of one section."""

    def __init__(self, section: _Section, config: ChunkerConfig) -> None:
        self._section = section
        self._size = section.size
        self._target = config.target_words
        self._min = config.min_words
        self._max = config.max_words
        self._overlap = round(config.target_words * config.overlap_percent / 100)

    def _row(self, position: int) -> int:
        """Index of the table row that holds the word at `position`, _NO_ROW for prose."""
        block = self._section.block_of[position]
        return block if self._section.block_is_row[block] else _NO_ROW

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
        row = self._row(position - 1)
        return row == _NO_ROW or row != self._row(position)

    def _is_oversized_row_start(self, position: int) -> bool:
        if position >= self._size or self._row(position) == _NO_ROW or not self._is_boundary(position):
            return False
        return self._section.block_words[self._row(position)] > self._max

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
