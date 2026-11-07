"""Text normalization applied before the chunker (REQ-010).

Removed: control characters (Unicode category Cc) except newline and tab, repeated spaces and tabs, trailing blanks of a
line, repeated blank lines and consecutive identical paragraphs. The text is put in Unicode NFC. Letters, accents,
digits, codes and punctuation are never touched: no de-hyphenation and no case change.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Sequence
from dataclasses import replace

from app.ingestion.blocks import Block

_CONTROL_CHARACTERS = {code: None for code in (*range(0x20), *range(0x7F, 0xA0)) if code not in (0x09, 0x0A)}
_BLANK_RUN = re.compile(r"[ \t]{2,}")
_TRAILING_BLANKS = re.compile(r"[ \t]+$", re.MULTILINE)
_EXTRA_NEWLINES = re.compile(r"\n{3,}")


def normalize_text(text: str) -> str:
    """Normalize a text; paragraphs stay separated by exactly one blank line."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = unicodedata.normalize("NFC", text.translate(_CONTROL_CHARACTERS))
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
