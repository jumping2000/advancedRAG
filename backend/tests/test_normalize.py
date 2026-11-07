# SPEC-2026-11-07-fase-2-ingestion
"""Normalization of the text before the chunker: AC-020 (REQ-010).

Interface of the plan: normalize_text(text) -> str and normalize_blocks(blocks) -> list[Block].
"""

from __future__ import annotations

import unicodedata

from app.ingestion.blocks import Block
from app.ingestion.normalize import normalize_blocks, normalize_text

# Fixtures use \u escapes, never literal characters, so that no editor or formatter can change their form.
E_ACUTE = "\u00e9"
A_GRAVE = "\u00e0"
E_NFD = "e\u0301"


def test_AC_020_text_loses_control_characters_repeats_and_duplicate_paragraphs() -> None:
    raw = (
        "Primo\x00 para\x07grafo  con   spazi\t\tripetuti e\x1b controllo.\n"
        "\n\n\n"
        f"Secondo paragrafo: perch{E_NFD} citt{A_GRAVE} ripetuto.\n"
        "\n"
        f"Secondo paragrafo: perch{E_NFD} citt{A_GRAVE} ripetuto.\n"
        "\n\n\n\n"
        "Fine: codice AB-12/3, 3,14% e co-operazione (trattino).\n"
    )

    result = normalize_text(raw)

    assert result == (
        "Primo paragrafo con spazi ripetuti e controllo.\n\n"
        f"Secondo paragrafo: perch{E_ACUTE} citt{A_GRAVE} ripetuto.\n\n"
        "Fine: codice AB-12/3, 3,14% e co-operazione (trattino)."
    )


def test_AC_020_accented_letters_end_in_nfc() -> None:
    result = normalize_text(f"perch{E_NFD} citt{A_GRAVE} piu\u0300")

    assert result == f"perch{E_ACUTE} citt{A_GRAVE} pi\u00f9"
    assert unicodedata.is_normalized("NFC", result)


def test_AC_020_newline_and_single_tab_are_kept() -> None:
    assert normalize_text("a\tb\nc") == "a\tb\nc"


def test_AC_020_windows_and_old_mac_line_ends_become_newlines() -> None:
    assert normalize_text("uno\r\ndue\rtre") == "uno\ndue\ntre"


def test_AC_020_letters_digits_codes_and_punctuation_are_untouched() -> None:
    text = (
        "ISO-9001:2015, art. 5/b, v1.2.3 \u2014 \u00abCi\u00f2\u00bb; "
        "e-mail: Mario.Rossi@Example.IT (OK!) inter-\nnazionale"
    )

    assert normalize_text(text) == text


def test_AC_020_not_consecutive_repeated_paragraphs_are_kept() -> None:
    text = "Alfa\n\nBeta\n\nAlfa"

    assert normalize_text(text) == text


def test_AC_020_whitespace_only_blank_lines_count_as_blank() -> None:
    assert normalize_text("uno\n   \n\t\n \ndue") == "uno\n\ndue"


def test_AC_020_empty_and_whitespace_only_texts_give_the_empty_string() -> None:
    assert normalize_text("") == ""
    assert normalize_text(" \t\n\n  \x00\x07 \n") == ""


def test_AC_020_blocks_are_normalized_one_by_one_and_keep_kind_level_and_page() -> None:
    blocks = [
        Block("heading", f"  Titolo{E_NFD}\x00  uno ", level=2, page=3),
        Block("paragraph", "Testo   con\x07 spazi.", page=3),
        Block("table_row", "a  |  b\x00 | c", page=4),
    ]

    assert normalize_blocks(blocks) == [
        Block("heading", f"Titolo{E_ACUTE} uno", level=2, page=3),
        Block("paragraph", "Testo con spazi.", page=3),
        Block("table_row", "a | b | c", page=4),
    ]


def test_AC_020_empty_blocks_are_dropped() -> None:
    blocks = [
        Block("paragraph", ""),
        Block("paragraph", "  \n\t "),
        Block("heading", "\x00\x01", level=1),
        Block("table_row", " "),
        Block("paragraph", "Resta"),
    ]

    assert normalize_blocks(blocks) == [Block("paragraph", "Resta")]


def test_AC_020_consecutive_identical_paragraph_blocks_are_dropped_keeping_the_first() -> None:
    blocks = [
        Block("paragraph", "Stesso testo.", page=1),
        Block("paragraph", "Stesso  testo.", page=2),
        Block("paragraph", "Altro."),
        Block("paragraph", "Stesso testo."),
    ]

    assert normalize_blocks(blocks) == [
        Block("paragraph", "Stesso testo.", page=1),
        Block("paragraph", "Altro."),
        Block("paragraph", "Stesso testo."),
    ]


def test_AC_020_identical_paragraphs_separated_by_a_heading_are_kept() -> None:
    blocks = [
        Block("paragraph", "Uguale"),
        Block("heading", "Titolo", level=1),
        Block("paragraph", "Uguale"),
    ]

    assert normalize_blocks(blocks) == blocks


def test_AC_020_identical_paragraphs_become_consecutive_when_an_empty_block_between_is_dropped() -> None:
    blocks = [Block("paragraph", "Uguale"), Block("paragraph", " "), Block("paragraph", "Uguale")]

    assert normalize_blocks(blocks) == [Block("paragraph", "Uguale")]


def test_AC_020_identical_table_rows_are_data_and_are_kept() -> None:
    rows = [Block("table_row", "x | 1"), Block("table_row", "x | 1")]

    assert normalize_blocks(rows) == rows


def test_AC_020_whitespace_class_control_characters_become_one_space() -> None:
    raw = "uno\x0bdue\x0ctre\x1cquattro\x1dcinque\x1esei\x1fsette\x85otto"

    assert normalize_text(raw) == "uno due tre quattro cinque sei sette otto"


def test_AC_020_spaces_around_a_whitespace_class_control_character_collapse_with_it() -> None:
    assert normalize_text("a \x0c b") == "a b"
    assert normalize_text("a\x0c\x0cb") == "a b"
    assert normalize_text("a\x0b\tb") == "a b"


def test_AC_020_the_other_control_characters_are_deleted_without_a_space() -> None:
    assert normalize_text("para\x07grafo\x00 x\x1by\x7fz\x9f") == "paragrafo xyz"


def test_AC_020_zero_width_space_bom_and_soft_hyphen_are_removed_everywhere() -> None:
    assert normalize_text("a​b﻿c­d") == "abcd"
    assert normalize_text("﻿inizio﻿") == "inizio"
    assert normalize_text("co­operazione ​​ fine") == "cooperazione fine"


def test_AC_020_the_other_format_characters_stay() -> None:
    text = "a‌b‍c \U0001f468‍\U0001f469  ⁠"

    assert normalize_text(text) == text


def test_AC_020_invisible_characters_are_removed_in_blocks_too() -> None:
    blocks = [Block("heading", "Tit​olo", level=1), Block("paragraph", "﻿un­ito"), Block("paragraph", "​")]

    assert normalize_blocks(blocks) == [Block("heading", "Titolo", level=1), Block("paragraph", "unito")]
