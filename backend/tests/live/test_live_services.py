# SPEC-2026-11-07-fase-2-ingestion
"""Live smoke tests of the real Docling and Ollama.

The plan (Risks) says the shape of Docling's answer rests on its documentation and on fixtures, not on your instance.
These tests are never part of the normal run or of CI: they are skipped unless `LIVE_SERVICES=1`, and the marker `live`
keeps them out of the default `pytest` run (pyproject.toml).

Run them once by hand before using the system, against the services you really use:

    LIVE_SERVICES=1 LIVE_DOCLING_BASE_URL=http://docling:5001 LIVE_OLLAMA_BASE_URL=http://ollama:11434 \
    LIVE_EMBEDDING_MODEL=bge-m3 [LIVE_DOCLING_API_KEY=...] uv run --directory backend pytest -m live

They convert a tiny PDF and a tiny DOCX, both generated here (a PDF written by hand, a DOCX built with zipfile), through
`DoclingClient`, and embed one text through `OllamaEmbedder`. Nothing is stored.
"""

from __future__ import annotations

import io
import math
import os
import zipfile
from pathlib import Path

import pytest

from app.ingestion.blocks import Block
from app.ingestion.docling import DoclingClient
from app.ingestion.embeddings import OllamaEmbedder

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(os.environ.get("LIVE_SERVICES") != "1", reason="set LIVE_SERVICES=1 to call the real services"),
]

SENTENCE = "The quick brown fox jumps over the lazy dog while the committee reviews the quarterly budget."
HEADING = "Quarterly Report"


def required(name: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        pytest.skip(f"{name} is not set")
    return value


def docling_client() -> DoclingClient:
    return DoclingClient(
        required("LIVE_DOCLING_BASE_URL"), os.environ.get("LIVE_DOCLING_API_KEY") or None, timeout=300.0
    )


def minimal_pdf(text: str) -> bytes:
    """A one-page PDF with the text in Helvetica, with a correct cross-reference table."""
    stream = f"BT /F1 14 Tf 72 720 Td ({text}) Tj ET".encode("ascii")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R"
        b" /Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(out.tell())
        out.write(f"{number} 0 obj\n".encode() + body + b"\nendobj\n")
    xref = out.tell()
    out.write(f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode())
    for offset in offsets:
        out.write(f"{offset:010d} 00000 n \n".encode())
    out.write(f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode())
    return out.getvalue()


def minimal_docx(heading: str, paragraph: str) -> bytes:
    """A DOCX with a heading and a paragraph: the parts Word requires, written by hand."""
    word = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/word/document.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
        '<Override PartName="/word/styles.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>'
        "</Types>"
    )
    relationships = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
        'Target="word/document.xml"/></Relationships>'
    )
    document_relationships = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>'
        "</Relationships>"
    )
    styles = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<w:styles xmlns:w="{word}">'
        '<w:style w:type="paragraph" w:default="1" w:styleId="Normal"><w:name w:val="Normal"/></w:style>'
        '<w:style w:type="paragraph" w:styleId="Heading1"><w:name w:val="heading 1"/><w:basedOn w:val="Normal"/>'
        "</w:style></w:styles>"
    )
    body = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<w:document xmlns:w="{word}"><w:body>'
        f'<w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr><w:r><w:t>{heading}</w:t></w:r></w:p>'
        f"<w:p><w:r><w:t>{paragraph}</w:t></w:r></w:p>"
        "</w:body></w:document>"
    )
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", content_types)
        archive.writestr("_rels/.rels", relationships)
        archive.writestr("word/_rels/document.xml.rels", document_relationships)
        archive.writestr("word/styles.xml", styles)
        archive.writestr("word/document.xml", body)
    return out.getvalue()


def words(blocks: list[Block]) -> str:
    return " ".join(block.text for block in blocks)


async def test_live_docling_converts_a_pdf_into_blocks_with_its_text(tmp_path: Path) -> None:
    path = tmp_path / "live.pdf"
    path.write_bytes(minimal_pdf(SENTENCE))

    blocks = await docling_client().convert(path, "live.pdf", "pdf")

    assert blocks, "Docling returned no block for a PDF with one line of text"
    assert "quick brown fox" in words(blocks)
    assert all(block.kind in ("heading", "paragraph", "table_row") for block in blocks)
    assert {block.page for block in blocks} <= {1, None}


async def test_live_docling_converts_a_docx_into_blocks_with_its_text(tmp_path: Path) -> None:
    path = tmp_path / "live.docx"
    path.write_bytes(minimal_docx(HEADING, SENTENCE))

    blocks = await docling_client().convert(path, "live.docx", "docx")

    assert blocks, "Docling returned no block for a DOCX with a heading and a paragraph"
    assert "quick brown fox" in words(blocks)
    assert HEADING in words(blocks)


async def test_live_ollama_embeds_one_text_into_one_finite_vector() -> None:
    embedder = OllamaEmbedder(required("LIVE_OLLAMA_BASE_URL"), required("LIVE_EMBEDDING_MODEL"), timeout=120.0)

    vectors = await embedder.embed([SENTENCE])

    assert len(vectors) == 1
    assert len(vectors[0]) > 0
    assert all(math.isfinite(number) for number in vectors[0])
