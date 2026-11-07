# SPEC-2026-11-07-fase-2-ingestion
"""Direct reading of TXT and MD: AC-018 (REQ-008, text part) and the plan's review focus for T-02.

Interface of the plan: read_text_document(data, fmt) -> list[Block]; the `#` headings give the section titles, the
lines starting with `|` are table rows.
"""

from __future__ import annotations

from app.ingestion.blocks import Block
from app.ingestion.chunker import ChunkerConfig, chunk_blocks
from app.ingestion.normalize import normalize_blocks
from app.ingestion.readers import read_text_document

DEFAULTS = ChunkerConfig(target_words=300, min_words=50, max_words=450, overlap_percent=15)


def md(text: str) -> list[Block]:
    return read_text_document(text.encode("utf-8"), "md")


def txt(text: str) -> list[Block]:
    return read_text_document(text.encode("utf-8"), "txt")


def test_AC_018_markdown_headings_and_paragraphs_become_blocks() -> None:
    text = "# Guida\n\nPrimo uno.\n\nSecondo uno.\n\n## Installazione\n\nPrimo due.\n\nSecondo due.\n"

    assert md(text) == [
        Block("heading", "Guida", level=1),
        Block("paragraph", "Primo uno."),
        Block("paragraph", "Secondo uno."),
        Block("heading", "Installazione", level=2),
        Block("paragraph", "Primo due."),
        Block("paragraph", "Secondo due."),
    ]


def test_AC_018_chunks_under_the_second_heading_have_the_path_as_section_title() -> None:
    text = (
        "# Guida\n\nPrimo paragrafo della guida.\n\nSecondo paragrafo della guida.\n\n"
        "## Installazione\n\nPrimo paragrafo di installazione.\n\nSecondo paragrafo di installazione.\n"
    )

    chunks = chunk_blocks(normalize_blocks(md(text)), DEFAULTS)

    assert [c.section_title for c in chunks] == ["Guida", "Guida > Installazione"]
    assert chunks[1].content == "Primo paragrafo di installazione.\n\nSecondo paragrafo di installazione."
    assert "Installazione" not in chunks[0].content


def test_AC_018_heading_levels_one_to_six_are_read_and_seven_hashes_are_text() -> None:
    text = "\n\n".join(f"{'#' * n} Livello {n}" for n in range(1, 8))

    assert md(text) == [Block("heading", f"Livello {n}", level=n) for n in range(1, 7)] + [
        Block("paragraph", "####### Livello 7")
    ]


def test_AC_018_hash_without_space_is_not_a_heading_and_closing_hashes_are_removed() -> None:
    text = "#hashtag\n\n## Titolo ##\n\n# C#\n\n#\n\n## \n"

    assert md(text) == [
        Block("paragraph", "#hashtag"),
        Block("heading", "Titolo", level=2),
        Block("heading", "C#", level=1),
    ]


def test_AC_018_a_heading_line_closes_the_paragraph_before_it() -> None:
    assert md("riga uno\nriga due\n# Titolo\nriga tre") == [
        Block("paragraph", "riga uno\nriga due"),
        Block("heading", "Titolo", level=1),
        Block("paragraph", "riga tre"),
    ]


def test_AC_018_table_rows_become_table_row_blocks_without_the_separator_row() -> None:
    text = "| Nome | Qta |\n|---|---|\n| mela | 3 |\n| pera | 4 |\n"

    assert md(text) == [
        Block("table_row", "Nome | Qta"),
        Block("table_row", "mela | 3"),
        Block("table_row", "pera | 4"),
    ]


def test_AC_018_table_separator_with_alignment_colons_is_dropped() -> None:
    assert md("| a | b |\n| :--- | ---: |\n| 1 | 2 |") == [Block("table_row", "a | b"), Block("table_row", "1 | 2")]


def test_AC_018_table_between_paragraphs_splits_them() -> None:
    assert md("Prima.\n| a | b |\n|--|--|\n| 1 | 2 |\nDopo.") == [
        Block("paragraph", "Prima."),
        Block("table_row", "a | b"),
        Block("table_row", "1 | 2"),
        Block("paragraph", "Dopo."),
    ]


def test_AC_018_fenced_code_block_stays_one_paragraph_even_with_hashes_pipes_and_blank_lines() -> None:
    fence = "```python\n# commento\n| non una riga |\n\nprint(1)\n```"

    assert md(f"Prima.\n\n{fence}\n\nDopo.") == [
        Block("paragraph", "Prima."),
        Block("paragraph", fence),
        Block("paragraph", "Dopo."),
    ]


def test_AC_018_unclosed_fence_runs_to_the_end_of_the_text() -> None:
    assert md("~~~\n# non titolo\ncodice") == [Block("paragraph", "~~~\n# non titolo\ncodice")]


def test_AC_018_list_items_stay_paragraph_text() -> None:
    assert md("- uno\n- due\n\n1. tre") == [Block("paragraph", "- uno\n- due"), Block("paragraph", "1. tre")]


def test_AC_018_front_matter_is_kept_as_one_paragraph_of_text() -> None:
    front = "---\ntitle: Guida\n# commento yaml\n\ntags: [a, b]\n---"

    assert md(f"{front}\n\n# Titolo\n\nTesto.") == [
        Block("paragraph", front),
        Block("heading", "Titolo", level=1),
        Block("paragraph", "Testo."),
    ]


def test_AC_018_dashes_not_at_the_top_or_without_a_closing_line_are_ordinary_text() -> None:
    assert md("Testo.\n\n---\n\nAltro.") == [
        Block("paragraph", "Testo."),
        Block("paragraph", "---"),
        Block("paragraph", "Altro."),
    ]
    assert md("---\n# Titolo\n") == [Block("paragraph", "---"), Block("heading", "Titolo", level=1)]


def test_AC_018_txt_has_paragraphs_separated_by_blank_lines_and_no_headings_or_tables() -> None:
    text = "# non un titolo\nseconda riga\n\n   \n| a | b |\n|---|---|\n\n\n\nUltimo."

    assert txt(text) == [
        Block("paragraph", "# non un titolo\nseconda riga"),
        Block("paragraph", "| a | b |\n|---|---|"),
        Block("paragraph", "Ultimo."),
    ]


def test_AC_018_utf8_bom_is_removed_for_txt_and_md() -> None:
    assert read_text_document(b"\xef\xbb\xbfCiao mondo", "txt") == [Block("paragraph", "Ciao mondo")]
    assert read_text_document(b"\xef\xbb\xbf# Titolo\n\nTesto", "md") == [
        Block("heading", "Titolo", level=1),
        Block("paragraph", "Testo"),
    ]


def test_AC_018_crlf_and_cr_line_ends_give_the_same_blocks_as_lf() -> None:
    lf = "# Titolo\n\nUno\ndue\n\n| a | b |\n|---|---|\n| 1 | 2 |\n"

    expected = md(lf)

    assert md(lf.replace("\n", "\r\n")) == expected
    assert md(lf.replace("\n", "\r")) == expected
    assert read_text_document(("\ufeff" + lf).replace("\n", "\r\n").encode("utf-8"), "md") == expected
    assert txt("uno\r\ndue\r\n\r\ntre") == [Block("paragraph", "uno\ndue"), Block("paragraph", "tre")]


def test_AC_018_a_line_of_100000_characters_without_spaces_is_one_block() -> None:
    long_line = "x" * 100_000

    assert txt(long_line) == [Block("paragraph", long_line)]
    assert md(long_line) == [Block("paragraph", long_line)]
    assert md("# " + long_line) == [Block("heading", long_line, level=1)]
    assert md("| " + long_line + " |") == [Block("table_row", long_line)]
    assert md("#" * 100_000 + "x") == [Block("paragraph", "#" * 100_000 + "x")]
    assert md("# " + "#" * 100_000 + "x") == [Block("heading", "#" * 100_000 + "x", level=1)]


def test_AC_018_a_line_of_100000_characters_gives_one_chunk_of_one_word() -> None:
    long_line = "x" * 100_000

    chunks = chunk_blocks(normalize_blocks(md(long_line)), DEFAULTS)

    assert len(chunks) == 1
    assert chunks[0].content == long_line
    assert chunks[0].word_count == 1


def test_AC_018_empty_and_whitespace_only_input_give_no_blocks_and_no_chunks() -> None:
    for fmt in ("txt", "md"):
        for data in (b"", b"\n", b" \t\r\n\r\n  \n", b"\xef\xbb\xbf", b"\xef\xbb\xbf\r\n"):
            blocks = read_text_document(data, fmt)
            assert blocks == []
            assert chunk_blocks(normalize_blocks(blocks), DEFAULTS) == []


def test_AC_018_bom_crlf_and_control_characters_do_not_reach_the_chunks() -> None:
    clean = "# Guida\n\nPrimo paragrafo.\n\nSecondo paragrafo.\n"
    dirty = "\ufeff# Guida\r\n\r\nPrimo\x00 para\x07grafo.\r\n\r\n\r\n\r\nSecondo  paragrafo.\r\n"

    expected = chunk_blocks(normalize_blocks(md(clean)), DEFAULTS)
    chunks = chunk_blocks(normalize_blocks(read_text_document(dirty.encode("utf-8"), "md")), DEFAULTS)

    assert chunks == expected
    assert [c.content for c in chunks] == ["Primo paragrafo.\n\nSecondo paragrafo."]
    assert all("\ufeff" not in c.content and "\r" not in c.content and "\x00" not in c.content for c in chunks)


def test_AC_018_front_matter_with_lists_indentation_comments_and_blank_lines_is_recognised() -> None:
    front = '---\ntitle: "Guida: base"\ntags:\n  - uno\n  - due\n- voce\n# commento\n\nautore:   Mario\n---'

    assert md(f"{front}\n\n# Titolo") == [Block("paragraph", front), Block("heading", "Titolo", level=1)]


def test_AC_018_front_matter_may_close_with_three_dots() -> None:
    front = "---\n# commento\ntitle: x\n..."

    assert md(f"{front}\n\nTesto") == [Block("paragraph", front), Block("paragraph", "Testo")]


def test_AC_018_a_rule_followed_by_headings_and_prose_and_another_rule_is_not_front_matter() -> None:
    text = "---\n\n# Titolo\n\nUn paragrafo che non e YAML.\n\n## Altro\n\n---\n\nFine."

    assert md(text) == [
        Block("paragraph", "---"),
        Block("heading", "Titolo", level=1),
        Block("paragraph", "Un paragrafo che non e YAML."),
        Block("heading", "Altro", level=2),
        Block("paragraph", "---"),
        Block("paragraph", "Fine."),
    ]


def test_AC_018_lines_between_the_rules_that_are_not_yaml_keep_the_file_a_normal_markdown_text() -> None:
    assert md("---\n# Titolo\nTesto libero senza chiave\n---\n") == [
        Block("paragraph", "---"),
        Block("heading", "Titolo", level=1),
        Block("paragraph", "Testo libero senza chiave\n---"),
    ]


def test_AC_018_front_matter_closing_within_150_lines_is_recognised() -> None:
    body = "\n".join(f"k{i}: v" for i in range(148))
    front = f"---\n# Titolo\n{body}\n---"

    assert md(front) == [Block("paragraph", front)]


def test_AC_018_front_matter_closing_after_250_lines_is_not_front_matter() -> None:
    body = "\n".join(f"k{i}: v" for i in range(250))

    blocks = md(f"---\n# Titolo\n{body}\n---\n")

    assert blocks[:2] == [Block("paragraph", "---"), Block("heading", "Titolo", level=1)]


def test_AC_018_a_backtick_in_the_info_string_of_a_backtick_fence_makes_it_ordinary_text() -> None:
    assert md("```a`b\n# titolo") == [Block("paragraph", "```a`b"), Block("heading", "titolo", level=1)]


def test_AC_018_a_closing_fence_longer_than_the_opening_one_closes_the_block() -> None:
    fence = "```\n# x\n`````"

    assert md(f"{fence}\n# dopo") == [Block("paragraph", fence), Block("heading", "dopo", level=1)]


def test_AC_018_a_shorter_closing_fence_does_not_close_the_block() -> None:
    assert md("`````\n# x\n```\n# ancora dentro") == [Block("paragraph", "`````\n# x\n```\n# ancora dentro")]


def test_AC_018_headings_indented_by_more_than_three_spaces_are_text() -> None:
    assert md("    # codice") == [Block("paragraph", "# codice")]
    assert md("riga\n    # dentro") == [Block("paragraph", "riga\n    # dentro")]
    assert md("   # titolo") == [Block("heading", "titolo", level=1)]


def test_AC_018_a_table_row_with_only_empty_cells_is_dropped() -> None:
    assert md("| a | b |\n|---|---|\n| | |\n| 1 | 2 |") == [Block("table_row", "a | b"), Block("table_row", "1 | 2")]


def test_AC_018_a_data_row_made_of_hyphens_is_not_the_delimiter_row() -> None:
    assert md("| a | b |\n|---|---|\n| - | - |\n| 1 | 2 |") == [
        Block("table_row", "a | b"),
        Block("table_row", "- | -"),
        Block("table_row", "1 | 2"),
    ]


def test_AC_018_only_the_second_row_of_a_table_is_the_delimiter_row() -> None:
    assert md("| a | b |\n|---|---|\n|---|---|") == [Block("table_row", "a | b"), Block("table_row", "--- | ---")]
    assert md("| - | - |\n| 1 | 2 |") == [Block("table_row", "- | -"), Block("table_row", "1 | 2")]


def test_AC_018_every_table_has_its_own_delimiter_row() -> None:
    assert md("| a |\n|---|\n\n| b |\n|:-:|\n| c |") == [
        Block("table_row", "a"),
        Block("table_row", "b"),
        Block("table_row", "c"),
    ]
