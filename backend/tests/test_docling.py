# SPEC-2026-11-07-fase-2-ingestion
"""Docling in the worker: AC-017 (REQ-008), and the classification of its errors for AC-031, AC-032, AC-033 (REQ-016).

Two halves. `docling_json_to_blocks` reads a DoclingDocument: the fixtures are a reduced copy of a real one (a DOCX of
the docling project converted by docling, schema 1.10.0) and a variant written by hand with pages, headings and a
table. `DoclingClient` is tested against the fake Docling on a real local socket (never a real service): the request
it sends (`POST /v1/convert/file`, form fields, the `X-Api-Key` header only when a key is set) and how every failure is
classified. A temporary error (`TemporaryError`) may succeed if repeated; a `PermanentError` cannot. The messages are
fixed text: they never carry the file name, the content of the file or what Docling says.
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest

from app.documents.formats import DocumentFormat
from app.ingestion.blocks import Block
from app.ingestion.docling import DoclingClient, docling_json_to_blocks
from app.ingestion.errors import IngestionError, PermanentError, TemporaryError
from tests.fakes.http_server import closed_port_url
from tests.fakes.services import fake_docling, fake_document_file

SECRET = "contenuto-riservato-del-documento"

# A reduced copy of a real DoclingDocument (the DOCX `unit_test_formatting` of the docling project, converted by
# docling): `body.children` lists `$ref`s to texts and groups in reading order; inline groups hold the runs of one
# paragraph, lists hold list items that may have children; a header/footer lives in groups of the `furniture` layer
# that are nevertheless listed in the body, with a picture; the DOCX has no page numbers (`prov` is empty).
REAL_SAMPLE: dict[str, Any] = {
    "schema_name": "DoclingDocument",
    "version": "1.10.0",
    "name": "unit_test_formatting",
    "origin": {
        "mimetype": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "filename": "unit_test_formatting.docx",
    },
    "furniture": {
        "self_ref": "#/furniture",
        "children": [],
        "content_layer": "furniture",
        "name": "_root_",
        "label": "unspecified",
    },
    "body": {
        "self_ref": "#/body",
        "children": [
            {"$ref": "#/texts/0"},
            {"$ref": "#/texts/1"},
            {"$ref": "#/groups/0"},
            {"$ref": "#/texts/5"},
            {"$ref": "#/groups/1"},
            {"$ref": "#/texts/14"},
            {"$ref": "#/texts/15"},
            {"$ref": "#/groups/5"},
            {"$ref": "#/groups/6"},
            {"$ref": "#/groups/7"},
        ],
        "content_layer": "body",
        "name": "_root_",
        "label": "unspecified",
    },
    "groups": [
        {
            "self_ref": "#/groups/0",
            "parent": {"$ref": "#/body"},
            "children": [{"$ref": "#/texts/2"}, {"$ref": "#/texts/3"}, {"$ref": "#/texts/4"}],
            "content_layer": "body",
            "name": "group",
            "label": "inline",
        },
        {
            "self_ref": "#/groups/1",
            "parent": {"$ref": "#/body"},
            "children": [{"$ref": "#/texts/6"}, {"$ref": "#/texts/7"}, {"$ref": "#/texts/8"}],
            "content_layer": "body",
            "name": "list",
            "label": "list",
        },
        {
            "self_ref": "#/groups/2",
            "parent": {"$ref": "#/texts/8"},
            "children": [{"$ref": "#/texts/9"}, {"$ref": "#/texts/10"}],
            "content_layer": "body",
            "name": "group",
            "label": "inline",
        },
        {
            "self_ref": "#/groups/3",
            "parent": {"$ref": "#/texts/8"},
            "children": [{"$ref": "#/texts/11"}],
            "content_layer": "body",
            "name": "list",
            "label": "list",
        },
        {
            "self_ref": "#/groups/4",
            "parent": {"$ref": "#/texts/11"},
            "children": [{"$ref": "#/texts/12"}, {"$ref": "#/texts/13"}],
            "content_layer": "body",
            "name": "group",
            "label": "inline",
        },
        {
            "self_ref": "#/groups/5",
            "parent": {"$ref": "#/body"},
            "children": [{"$ref": "#/texts/16"}],
            "content_layer": "furniture",
            "name": "page header",
            "label": "section",
        },
        {
            "self_ref": "#/groups/6",
            "parent": {"$ref": "#/body"},
            "children": [{"$ref": "#/texts/17"}],
            "content_layer": "furniture",
            "name": "page footer",
            "label": "section",
        },
        {
            "self_ref": "#/groups/7",
            "parent": {"$ref": "#/body"},
            "children": [
                {"$ref": "#/texts/18"},
                {"$ref": "#/texts/19"},
                {"$ref": "#/pictures/0"},
                {"$ref": "#/texts/20"},
            ],
            "content_layer": "furniture",
            "name": "page footer",
            "label": "section",
        },
    ],
    "texts": [
        {
            "self_ref": "#/texts/0",
            "parent": {"$ref": "#/body"},
            "children": [],
            "content_layer": "body",
            "label": "text",
            "prov": [],
            "text": "italic",
        },
        {
            "self_ref": "#/texts/1",
            "parent": {"$ref": "#/body"},
            "children": [],
            "content_layer": "body",
            "label": "text",
            "prov": [],
            "text": "bold",
        },
        {
            "self_ref": "#/texts/2",
            "parent": {"$ref": "#/groups/0"},
            "children": [],
            "content_layer": "body",
            "label": "text",
            "prov": [],
            "text": "Normal",
        },
        {
            "self_ref": "#/texts/3",
            "parent": {"$ref": "#/groups/0"},
            "children": [],
            "content_layer": "body",
            "label": "text",
            "prov": [],
            "text": "italic",
        },
        {
            "self_ref": "#/texts/4",
            "parent": {"$ref": "#/groups/0"},
            "children": [],
            "content_layer": "body",
            "label": "text",
            "prov": [],
            "text": "bold",
        },
        {
            "self_ref": "#/texts/5",
            "parent": {"$ref": "#/body"},
            "children": [],
            "content_layer": "body",
            "label": "text",
            "prov": [],
            "text": "",
        },
        {
            "self_ref": "#/texts/6",
            "parent": {"$ref": "#/groups/1"},
            "children": [],
            "content_layer": "body",
            "label": "list_item",
            "prov": [],
            "text": "Italic bullet 1",
        },
        {
            "self_ref": "#/texts/7",
            "parent": {"$ref": "#/groups/1"},
            "children": [],
            "content_layer": "body",
            "label": "list_item",
            "prov": [],
            "text": "Bold bullet 2",
        },
        {
            "self_ref": "#/texts/8",
            "parent": {"$ref": "#/groups/1"},
            "children": [{"$ref": "#/groups/2"}, {"$ref": "#/groups/3"}],
            "content_layer": "body",
            "label": "list_item",
            "prov": [],
            "text": "",
        },
        {
            "self_ref": "#/texts/9",
            "parent": {"$ref": "#/groups/2"},
            "children": [],
            "content_layer": "body",
            "label": "text",
            "prov": [],
            "text": "Some",
        },
        {
            "self_ref": "#/texts/10",
            "parent": {"$ref": "#/groups/2"},
            "children": [],
            "content_layer": "body",
            "label": "text",
            "prov": [],
            "text": "italic",
        },
        {
            "self_ref": "#/texts/11",
            "parent": {"$ref": "#/groups/3"},
            "children": [{"$ref": "#/groups/4"}],
            "content_layer": "body",
            "label": "list_item",
            "prov": [],
            "text": "",
        },
        {
            "self_ref": "#/texts/12",
            "parent": {"$ref": "#/groups/4"},
            "children": [],
            "content_layer": "body",
            "label": "text",
            "prov": [],
            "text": "Nested",
        },
        {
            "self_ref": "#/texts/13",
            "parent": {"$ref": "#/groups/4"},
            "children": [],
            "content_layer": "body",
            "label": "text",
            "prov": [],
            "text": "italic",
        },
        {
            "self_ref": "#/texts/14",
            "parent": {"$ref": "#/body"},
            "children": [],
            "content_layer": "body",
            "label": "text",
            "prov": [],
            "text": "Indexation",
        },
        {
            "self_ref": "#/texts/15",
            "parent": {"$ref": "#/body"},
            "children": [],
            "content_layer": "body",
            "label": "text",
            "prov": [],
            "text": "The second page of the document with same header and footer",
        },
        {
            "self_ref": "#/texts/16",
            "parent": {"$ref": "#/groups/5"},
            "children": [],
            "content_layer": "furniture",
            "label": "text",
            "prov": [],
            "text": "This is a header",
        },
        {
            "self_ref": "#/texts/17",
            "parent": {"$ref": "#/groups/6"},
            "children": [],
            "content_layer": "furniture",
            "label": "text",
            "prov": [],
            "text": "This is a footer",
        },
        {
            "self_ref": "#/texts/18",
            "parent": {"$ref": "#/groups/7"},
            "children": [],
            "content_layer": "furniture",
            "label": "text",
            "prov": [],
            "text": "Another footer",
        },
        {
            "self_ref": "#/texts/19",
            "parent": {"$ref": "#/groups/7"},
            "children": [],
            "content_layer": "furniture",
            "label": "text",
            "prov": [],
            "text": "With",
        },
        {
            "self_ref": "#/texts/20",
            "parent": {"$ref": "#/groups/7"},
            "children": [],
            "content_layer": "furniture",
            "label": "text",
            "prov": [],
            "text": "3 paragraphs and a picture",
        },
    ],
    "pictures": [
        {
            "self_ref": "#/pictures/0",
            "parent": {"$ref": "#/groups/7"},
            "children": [],
            "content_layer": "furniture",
            "label": "picture",
            "prov": [],
        }
    ],
    "tables": [],
    "key_value_items": [],
    "form_items": [],
    "pages": {},
}


def paragraphs(*texts: str) -> list[Block]:
    return [Block("paragraph", text) for text in texts]


# docling_json_to_blocks: the real sample ----------------------------------------------------------------------------


def test_AC_017_a_real_docling_document_is_read_depth_first_in_reading_order_without_furniture_or_pictures() -> None:
    blocks = docling_json_to_blocks(REAL_SAMPLE)

    # The runs of an inline group are one paragraph; empty texts and the list items without text give nothing but
    # their children are read; the header, the footer and the picture (furniture layer) are left out; no page numbers.
    assert blocks == paragraphs(
        "italic",
        "bold",
        "Normal italic bold",
        "Italic bullet 1",
        "Bold bullet 2",
        "Some italic",
        "Nested italic",
        "Indexation",
        "The second page of the document with same header and footer",
    )


def test_AC_017_the_reader_does_not_change_the_document_it_reads() -> None:
    before = copy.deepcopy(REAL_SAMPLE)

    docling_json_to_blocks(REAL_SAMPLE)

    assert REAL_SAMPLE == before


# docling_json_to_blocks: headings, pages and a table (written by hand) -----------------------------------------------


def text_node(index: int, label: str, text: str, pages: list[object] | None = None, **extra: Any) -> dict[str, Any]:
    prov = [{"page_no": page, "bbox": {}, "charspan": [0, len(text)]} for page in pages or []]
    return {
        "self_ref": f"#/texts/{index}",
        "parent": {"$ref": "#/body"},
        "children": [],
        "content_layer": "body",
        "label": label,
        "prov": prov,
        "orig": text,
        "text": text,
        **extra,
    }


def table_node(index: int, rows: list[list[str]], pages: list[int]) -> dict[str, Any]:
    return {
        "self_ref": f"#/tables/{index}",
        "parent": {"$ref": "#/body"},
        "children": [],
        "content_layer": "body",
        "label": "table",
        "prov": [{"page_no": page, "bbox": {}, "charspan": [0, 0]} for page in pages],
        "captions": [],
        "data": {
            "num_rows": len(rows),
            "num_cols": len(rows[0]),
            "table_cells": [],  # the project reads `grid`
            "grid": [[{"text": cell, "row_span": 1, "col_span": 1} for cell in row] for row in rows],
        },
    }


def document(body: list[str], **collections: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "schema_name": "DoclingDocument",
        "version": "1.10.0",
        "name": "by hand",
        "furniture": {"self_ref": "#/furniture", "children": [], "content_layer": "furniture", "label": "unspecified"},
        "body": {
            "self_ref": "#/body",
            "children": [{"$ref": ref} for ref in body],
            "content_layer": "body",
            "label": "unspecified",
        },
        "groups": collections.get("groups", []),
        "texts": collections.get("texts", []),
        "pictures": collections.get("pictures", []),
        "tables": collections.get("tables", []),
        "pages": {},
    }


def paged_document() -> dict[str, Any]:
    texts = [
        text_node(0, "title", "Manuale d'uso", [1]),
        text_node(1, "section_header", "Introduzione", [1], level=1),
        text_node(2, "text", "Questo manuale spiega il prodotto.", [1]),
        text_node(3, "section_header", "Dettagli", [2], level=2),
        text_node(4, "text", "Un paragrafo che comincia a pagina due e finisce a pagina tre.", [2, 3]),
        text_node(5, "list_item", "Primo punto dell'elenco", [3]),
        text_node(6, "caption", "Tabella 1: i prezzi", [3]),
        text_node(7, "code", "make install", [3]),
        text_node(8, "text", "Un testo senza pagina"),
        text_node(9, "page_header", "Intestazione ripetuta", [1], content_layer="furniture"),
    ]
    tables = [table_node(0, [["Voce", "Prezzo"], ["Caffè", "1,20"], ["Tè", "0,90"]], [3])]
    pictures = [
        {
            "self_ref": "#/pictures/0",
            "parent": {"$ref": "#/body"},
            "children": [],
            "content_layer": "body",
            "label": "picture",
            "prov": [{"page_no": 2, "bbox": {}, "charspan": [0, 0]}],
        }
    ]
    body = [f"#/texts/{n}" for n in range(0, 4)] + ["#/pictures/0", "#/texts/4", "#/texts/5", "#/tables/0"]
    body += ["#/texts/6", "#/texts/7", "#/texts/8", "#/texts/9"]
    return document(body, texts=texts, tables=tables, pictures=pictures)


def test_AC_017_headings_get_their_level_paragraphs_their_page_and_a_table_one_block_per_row() -> None:
    blocks = docling_json_to_blocks(paged_document())

    assert blocks == [
        Block("heading", "Manuale d'uso", level=1, page=1),
        Block("heading", "Introduzione", level=1, page=1),
        Block("paragraph", "Questo manuale spiega il prodotto.", page=1),
        Block("heading", "Dettagli", level=2, page=2),
        Block("paragraph", "Un paragrafo che comincia a pagina due e finisce a pagina tre.", page=2),
        Block("paragraph", "Primo punto dell'elenco", page=3),
        Block("table_row", "Voce | Prezzo", page=3),
        Block("table_row", "Caffè | 1,20", page=3),
        Block("table_row", "Tè | 0,90", page=3),
        Block("paragraph", "Tabella 1: i prezzi", page=3),
        Block("paragraph", "make install", page=3),
        Block("paragraph", "Un testo senza pagina", page=None),
    ]


def test_AC_017_the_title_is_a_level_1_heading_and_a_header_without_level_is_level_1() -> None:
    texts = [text_node(0, "title", "Titolo", [1]), text_node(1, "section_header", "Senza livello", [1])]
    blocks = docling_json_to_blocks(document(["#/texts/0", "#/texts/1"], texts=texts))

    assert [(block.kind, block.level) for block in blocks] == [("heading", 1), ("heading", 1)]


@pytest.mark.parametrize(("level", "expected"), [(3, 3), (0, 1), (-2, 1), ("2", 1), (None, 1)])
def test_AC_017_the_level_of_a_section_header_is_read_from_level_and_never_below_1(
    level: object, expected: int
) -> None:
    texts = [text_node(0, "section_header", "Sezione", [1], level=level)]

    (block,) = docling_json_to_blocks(document(["#/texts/0"], texts=texts))

    assert (block.kind, block.level) == ("heading", expected)


@pytest.mark.parametrize(
    ("pages", "expected"),
    [([], None), ([4], 4), ([4, 5], 4), (["x", 6], 6), (["x"], None), ([None], None), ([True], None), ([0], None)],
)
def test_AC_017_the_page_is_the_first_page_number_found_in_prov(pages: list[object], expected: int | None) -> None:
    texts = [text_node(0, "text", "Un paragrafo", pages)]

    (block,) = docling_json_to_blocks(document(["#/texts/0"], texts=texts))

    assert block.page == expected


def test_AC_017_the_rows_of_a_table_without_any_text_are_left_out() -> None:
    tables = [table_node(0, [["A", "B"], ["", ""], ["C", "D"]], [1])]

    blocks = docling_json_to_blocks(document(["#/tables/0"], tables=tables))

    assert blocks == [Block("table_row", "A | B", page=1), Block("table_row", "C | D", page=1)]


def test_AC_017_the_furniture_root_is_not_read_even_when_it_lists_texts() -> None:
    doc = document(
        ["#/texts/0"], texts=[text_node(0, "text", "Corpo", [1]), text_node(1, "text", "Piè di pagina", [1])]
    )
    doc["furniture"]["children"] = [{"$ref": "#/texts/1"}]

    assert docling_json_to_blocks(doc) == [Block("paragraph", "Corpo", page=1)]


# docling_json_to_blocks: documents that are not as expected ---------------------------------------------------------


@pytest.mark.parametrize(
    "broken",
    [
        {},
        {"body": None},
        {"body": {}},
        {"body": {"children": None}},
        {"body": {"children": "#/texts/0"}},
        {"body": {"children": [None, 3, "x", {"$ref": 7}, {"$ref": "#/nowhere/0"}, {"$ref": "#/texts/x"}]}},
        {"body": {"children": [{"$ref": "#/texts/5"}, {"$ref": "#/texts/-1"}]}, "texts": [{"text": "unico"}]},
        {"texts": "not a list", "body": {"children": [{"$ref": "#/texts/0"}]}},
        {"texts": ["not a node"], "body": {"children": [{"$ref": "#/texts/0"}]}},
        {"texts": [{"label": "text", "text": 42}], "body": {"children": [{"$ref": "#/texts/0"}]}},
        {"tables": [{"data": "grid?"}], "body": {"children": [{"$ref": "#/tables/0"}]}},
        {"tables": [{"data": {"grid": "no"}}], "body": {"children": [{"$ref": "#/tables/0"}]}},
        {"tables": [{"data": {"grid": [None, "row", [None, 5]]}}], "body": {"children": [{"$ref": "#/tables/0"}]}},
    ],
)
def test_AC_017_a_document_with_missing_or_wrong_parts_gives_no_blocks_and_no_error(broken: dict[str, Any]) -> None:
    assert docling_json_to_blocks(broken) == []


def test_AC_017_a_document_that_is_not_a_mapping_gives_no_blocks() -> None:
    assert docling_json_to_blocks([]) == []  # type: ignore[arg-type]


def test_AC_017_what_can_be_read_is_read_around_what_cannot() -> None:
    doc = {
        "body": {"children": [{"$ref": "#/texts/0"}, {"$ref": "#/texts/9"}, {"$ref": "#/texts/1"}, None]},
        "texts": [{"label": "text", "text": "uno"}, {"label": "text", "text": "due", "prov": "pagine?"}],
    }

    assert docling_json_to_blocks(doc) == paragraphs("uno", "due")


def test_AC_017_references_that_loop_are_read_once_and_end() -> None:
    doc = {
        "body": {"children": [{"$ref": "#/groups/0"}, {"$ref": "#/texts/0"}]},
        "groups": [
            {"label": "list", "children": [{"$ref": "#/texts/0"}, {"$ref": "#/groups/1"}]},
            {"label": "list", "children": [{"$ref": "#/groups/0"}, {"$ref": "#/groups/1"}]},
        ],
        "texts": [{"label": "list_item", "text": "voce", "children": [{"$ref": "#/groups/0"}, {"$ref": "#/texts/0"}]}],
    }

    assert docling_json_to_blocks(doc) == paragraphs("voce")


def test_AC_017_a_very_deep_document_does_not_crash_the_reader() -> None:
    depth = 5000
    groups = [{"label": "list", "children": [{"$ref": f"#/groups/{n + 1}"}]} for n in range(depth)]
    groups.append({"label": "list", "children": [{"$ref": "#/texts/0"}]})
    doc = {"body": {"children": [{"$ref": "#/groups/0"}]}, "groups": groups, "texts": [{"label": "text", "text": "x"}]}

    blocks = docling_json_to_blocks(doc)

    assert len(blocks) <= 1  # the reader may stop descending, but it ends and does not raise


# DoclingClient: the request ------------------------------------------------------------------------------------------

ITEMS = [("title", "Titolo del documento", 1), ("text", "Un paragrafo di prova che si legge.", 1)]
FORMATS: list[DocumentFormat] = ["pdf", "docx", "pptx", "xlsx"]


def write_original(folder: Path, content: bytes) -> Path:
    path = folder / "original"
    path.write_bytes(content)
    return path


def client_for(url: str, api_key: str | None = None, timeout: float = 5.0) -> DoclingClient:
    return DoclingClient(url, api_key, timeout)


@pytest.mark.parametrize("fmt", FORMATS)
async def test_AC_017_the_client_posts_the_file_to_convert_file_with_the_form_fields_and_the_key(
    tmp_path: Path, fmt: DocumentFormat
) -> None:
    content = fake_document_file(ITEMS)
    path = write_original(tmp_path, content)
    with fake_docling() as docling:
        blocks = await client_for(docling.url, "docling-key-123").convert(path, f"upload.{fmt}", fmt)

    (request,) = docling.requests
    assert request.content == content
    assert request.filename == f"upload.{fmt}"
    assert request.api_key == "docling-key-123"
    assert request.fields == {"from_formats": fmt, "to_formats": "json", "do_ocr": "false"}
    assert blocks == [
        Block("heading", "Titolo del documento", level=1, page=1),
        *[Block("paragraph", ITEMS[1][1], page=1)],
    ]


@pytest.mark.parametrize("api_key", [None, ""])
async def test_AC_017_without_a_key_no_x_api_key_header_is_sent(tmp_path: Path, api_key: str | None) -> None:
    path = write_original(tmp_path, fake_document_file(ITEMS))
    with fake_docling() as docling:
        await client_for(docling.url, api_key).convert(path, "upload.pdf", "pdf")

    assert [request.api_key for request in docling.requests] == [None]


async def test_AC_017_a_trailing_slash_in_the_base_url_does_not_change_the_endpoint(tmp_path: Path) -> None:
    path = write_original(tmp_path, fake_document_file(ITEMS))
    with fake_docling() as docling:
        await client_for(docling.url + "/").convert(path, "upload.pdf", "pdf")

    assert len(docling.requests) == 1


async def test_AC_017_a_partial_success_is_read_like_a_success(tmp_path: Path) -> None:
    path = write_original(tmp_path, fake_document_file(ITEMS))
    with fake_docling(result_status="partial_success") as docling:
        blocks = await client_for(docling.url).convert(path, "upload.pdf", "pdf")

    assert len(blocks) == 2


# DoclingClient: temporary errors (worth repeating) -------------------------------------------------------------------


async def convert_error(path: Path, url: str, limit: float = 5.0) -> IngestionError:
    with pytest.raises(IngestionError) as caught:
        await client_for(url, timeout=limit).convert(path, "segretissimo-contratto.pdf", "pdf")
    return caught.value


async def test_AC_032_a_refused_connection_is_a_temporary_docling_unreachable(tmp_path: Path) -> None:
    error = await convert_error(write_original(tmp_path, fake_document_file(ITEMS)), closed_port_url())

    assert isinstance(error, TemporaryError)
    assert error.code == "docling_unreachable"
    assert error.message == "The document parsing service could not be reached"


async def test_AC_032_a_timeout_is_a_temporary_docling_unreachable(tmp_path: Path) -> None:
    with fake_docling(delay_seconds=3.0) as docling:
        error = await convert_error(write_original(tmp_path, fake_document_file(ITEMS)), docling.url, limit=0.3)

    assert isinstance(error, TemporaryError)
    assert error.code == "docling_unreachable"
    assert len(docling.requests) == 1


async def test_AC_032_a_dropped_connection_is_a_temporary_docling_unreachable(tmp_path: Path) -> None:
    with fake_docling(drop_connection=True) as docling:
        error = await convert_error(write_original(tmp_path, fake_document_file(ITEMS)), docling.url)

    assert isinstance(error, TemporaryError)
    assert error.code == "docling_unreachable"


@pytest.mark.parametrize("status", [500, 502, 503, 504, 429])
async def test_AC_031_a_5xx_or_429_answer_is_a_temporary_docling_error(tmp_path: Path, status: int) -> None:
    with fake_docling(fail_status=status) as docling:
        error = await convert_error(write_original(tmp_path, fake_document_file(ITEMS)), docling.url)

    assert isinstance(error, TemporaryError)
    assert error.code == "docling_error"
    assert error.message == f"The document parsing service answered with an error ({status})"
    assert len(docling.requests) == 1  # the client does not repeat: with_retries does


# DoclingClient: permanent errors (no use repeating) ------------------------------------------------------------------


@pytest.mark.parametrize("status", [400, 401, 403, 404, 413, 415, 422])
async def test_AC_033_any_other_4xx_answer_is_a_permanent_parse_failed(tmp_path: Path, status: int) -> None:
    with fake_docling(fail_status=status) as docling:
        error = await convert_error(write_original(tmp_path, fake_document_file(ITEMS)), docling.url)

    assert isinstance(error, PermanentError)
    assert error.code == "parse_failed"
    assert error.message == "The document could not be parsed"
    assert len(docling.requests) == 1


async def test_AC_033_a_failure_status_in_a_200_answer_is_a_permanent_parse_failed_and_hides_what_docling_says(
    tmp_path: Path,
) -> None:
    with fake_docling(result_status="failure", error_text=f"cannot read {SECRET}") as docling:
        error = await convert_error(write_original(tmp_path, fake_document_file(ITEMS)), docling.url)

    assert isinstance(error, PermanentError)
    assert error.code == "parse_failed"
    assert error.message == "The document could not be parsed"
    assert SECRET not in error.message


@pytest.mark.parametrize("mode", [{"invalid_body": True}, {"result_status": "skipped"}])
async def test_AC_033_an_answer_without_a_document_is_a_permanent_docling_error(
    tmp_path: Path, mode: dict[str, Any]
) -> None:
    path = write_original(tmp_path, b"%PDF-1.4\n{}")  # a document with nothing in it: there is no json_content to read
    with fake_docling(**mode) as docling:
        error = await convert_error(path, docling.url)

    assert isinstance(error, PermanentError)
    assert error.code == "docling_error"
    assert error.message == "The document parsing service returned an invalid response"


async def test_AC_033_an_answer_that_is_not_json_is_a_permanent_docling_error(tmp_path: Path) -> None:
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            self.rfile.read(int(self.headers.get("Content-Length", "0")))
            body = b"<html>not json</html>"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:  # noqa: A002
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        error = await convert_error(
            write_original(tmp_path, fake_document_file(ITEMS)), f"http://127.0.0.1:{server.server_address[1]}"
        )
    finally:
        server.shutdown()
        server.server_close()

    assert isinstance(error, PermanentError)
    assert error.code == "docling_error"


async def test_AC_017_the_messages_never_carry_the_file_name_or_the_content(tmp_path: Path) -> None:
    content = fake_document_file([("text", SECRET, 1)])
    path = write_original(tmp_path, content)
    for modes in ({"fail_status": 503}, {"fail_status": 422}, {"result_status": "failure", "error_text": SECRET}):
        with fake_docling(**modes) as docling:
            error = await convert_error(path, docling.url)
        assert SECRET not in error.message
        assert "segretissimo" not in error.message
        assert SECRET not in str(error)
