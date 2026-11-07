"""Text normalization applied before the chunker (REQ-010).

Control characters (Unicode category Cc) other than newline and tab are deleted, except the ones that Python treats as
white space (U+000B, U+000C, U+001C..U+001F, U+0085), which become a space so that the words around them do not stick
together. U+200B (zero width space), U+FEFF (BOM) and U+00AD (soft hyphen) are deleted wherever they are; the other
format characters (U+200C, U+200D, ...) stay because they carry meaning in some scripts and in emoji. Also removed:
repeated spaces and tabs, trailing blanks of a line, repeated blank lines and consecutive identical paragraphs. The text
is put in Unicode NFC. Letters, accents, digits, codes and punctuation are never touched: no de-hyphenation and no case
change.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Sequence
from dataclasses import replace

from app.ingestion.blocks import Block

_WHITESPACE_CONTROLS = (0x0B, 0x0C, 0x1C, 0x1D, 0x1E, 0x1F, 0x85)
_INVISIBLE_CHARACTERS = (0x200B, 0xFEFF, 0x00AD)
_CHARACTER_MAP: dict[int, str | None] = {
    code: None
    for code in (*range(0x20), *range(0x7F, 0xA0), *_INVISIBLE_CHARACTERS)
    if code not in (0x09, 0x0A, *_WHITESPACE_CONTROLS)
}
_CHARACTER_MAP.update(dict.fromkeys(_WHITESPACE_CONTROLS, " "))
_BLANK_RUN = re.compile(r"[ \t]{2,}")
_TRAILING_BLANKS = re.compile(r"[ \t]+$", re.MULTILINE)
_EXTRA_NEWLINES = re.compile(r"\n{3,}")


def normalize_text(text: str) -> str:
    """Normalize a text; paragraphs stay separated by exactly one blank line."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = unicodedata.normalize("NFC", text.translate(_CHARACTER_MAP))
    text = _BLANK_RUN.sub(" ", text)
    text = _TRAILING_BLANKS.sub("", text)
    text = _EXTRA_NEWLINES.sub("\n\n", text).strip()
    paragraphs: list[str] = []
    for paragraph in text.split("\n\n"):
        if not paragraphs or paragraph != paragraphs[-1]:
            paragraphs.append(paragraph)
    return "\n\n".join(paragraphs)


def normalize_blocks(blocks: Sequence[Block]) -> list[Block]:
    """Normalize every block, drop the empty ones and the paragraphs identical to the previous paragraph."""
    result: list[Block] = []
    for block in blocks:
        text = normalize_text(block.text)
        if not text:
            continue
        if block.kind == "paragraph" and result and result[-1].kind == "paragraph" and result[-1].text == text:
            continue
        result.append(replace(block, text=text))
    return result
