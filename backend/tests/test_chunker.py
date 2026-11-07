# SPEC-2026-11-07-fase-2-ingestion
"""Structural chunker: AC-021, AC-022 and AC-023 (REQ-011).

Interface of the plan: chunk_blocks(blocks, ChunkerConfig(target_words, min_words, max_words, overlap_percent)) ->
list[ChunkDraft(position, section_title, page_start, page_end, content, word_count)]. A word is a run of non-white
characters; the heading text is the section title and not part of the content; the overlap between consecutive prose
chunks of a section is round(target_words * overlap_percent / 100) words (45 with the defaults); between table
chunks it is rounded up to whole rows.

The fixtures use unique words (`a0001`, `a0002`, ...) so that the words shared by two chunks are the overlap.
"""

from __future__ import annotations

from collections.abc import Sequence

from app.ingestion.blocks import Block
from app.ingestion.chunker import ChunkDraft, ChunkerConfig, chunk_blocks

DEFAULTS = ChunkerConfig(target_words=300, min_words=50, max_words=450, overlap_percent=15)


def words(prefix: str, count: int, start: int = 0) -> list[str]:
    return [f"{prefix}{i:04d}" for i in range(start, start + count)]


def paragraphs(prefix: str, count: int, size: int = 100) -> list[Block]:
    """`count` words in paragraphs of `size` words (the last one may be shorter)."""
    all_words = words(prefix, count)
    return [Block("paragraph", " ".join(all_words[i : i + size])) for i in range(0, count, size)]


def heading(text: str, level: int = 1) -> Block:
    return Block("heading", text, level=level)


def replace_page(block: Block, page: int | None) -> Block:
    return Block(block.kind, block.text, block.level, page)


def chunk_words(chunk: ChunkDraft) -> list[str]:
    return chunk.content.split()


def shared_prefix_suffix(previous: ChunkDraft, current: ChunkDraft) -> int:
    """Length of the longest suffix of `previous` that is a prefix of `current` (unique words: the overlap)."""
    a, b = chunk_words(previous), chunk_words(current)
    for k in range(min(len(a), len(b)), 0, -1):
        if a[-k:] == b[:k]:
            return k
    return 0


def join_without_overlap(chunks: Sequence[ChunkDraft]) -> list[str]:
    result: list[str] = []
    previous: ChunkDraft | None = None
    for chunk in chunks:
        skip = shared_prefix_suffix(previous, chunk) if previous is not None else 0
        result.extend(chunk_words(chunk)[skip:])
        previous = chunk
    return result


# ---------------------------------------------------------------------------------------------------------------------
# AC-021: sections of 700, 40 and 320 words with the default settings
# ---------------------------------------------------------------------------------------------------------------------


def three_sections() -> list[Block]:
    return [
        heading("Uno"),
        *paragraphs("a", 700),
        heading("Due"),
        *paragraphs("b", 40),
        heading("Tre"),
        *paragraphs("c", 320),
    ]


def test_AC_021_no_chunk_mixes_sections_and_none_exceeds_450_words() -> None:
    chunks = chunk_blocks(three_sections(), DEFAULTS)

    assert chunks
    for chunk in chunks:
        prefixes = {w[0] for w in chunk_words(chunk)}
        assert len(prefixes) == 1
        assert {"a": "Uno", "b": "Due", "c": "Tre"}[prefixes.pop()] == chunk.section_title
        assert chunk.word_count <= 450


def test_AC_021_consecutive_chunks_of_the_first_section_share_exactly_45_words() -> None:
    chunks = [c for c in chunk_blocks(three_sections(), DEFAULTS) if c.section_title == "Uno"]

    assert [c.word_count for c in chunks] == [300, 300, 190]
    for previous, current in zip(chunks, chunks[1:], strict=False):
        shared = set(chunk_words(previous)) & set(chunk_words(current))
        assert len(shared) == 45
        assert chunk_words(previous)[-45:] == chunk_words(current)[:45]
    assert join_without_overlap(chunks) == words("a", 700)


def test_AC_021_the_section_of_40_words_is_one_chunk_with_all_its_words() -> None:
    chunks = [c for c in chunk_blocks(three_sections(), DEFAULTS) if c.section_title == "Due"]

    assert len(chunks) == 1
    assert chunk_words(chunks[0]) == words("b", 40)
    assert chunks[0].word_count == 40


def test_AC_021_a_remainder_below_the_minimum_is_merged_into_the_previous_chunk() -> None:
    chunks = [c for c in chunk_blocks(three_sections(), DEFAULTS) if c.section_title == "Tre"]

    # 320 words: a second window would add only 20 new words (below 50), so one chunk of 320 words.
    assert [c.word_count for c in chunks] == [320]
    assert chunk_words(chunks[0]) == words("c", 320)


def test_AC_021_a_remainder_of_at_least_the_minimum_is_a_chunk_of_its_own() -> None:
    blocks = paragraphs("a", 350)

    chunks = chunk_blocks(blocks, DEFAULTS)

    assert [c.word_count for c in chunks] == [300, 95]
    assert join_without_overlap(chunks) == words("a", 350)


def test_AC_021_overlap_is_a_percentage_of_the_target_not_of_the_chunk() -> None:
    config = ChunkerConfig(target_words=100, min_words=20, max_words=150, overlap_percent=25)

    chunks = chunk_blocks(paragraphs("a", 300), config)

    assert [c.word_count for c in chunks] == [100, 100, 100, 75]
    for previous, current in zip(chunks, chunks[1:], strict=False):
        assert shared_prefix_suffix(previous, current) == 25
    assert join_without_overlap(chunks) == words("a", 300)


def test_AC_021_the_last_two_chunks_are_rebalanced_when_merging_would_exceed_the_maximum() -> None:
    config = ChunkerConfig(target_words=100, min_words=60, max_words=120, overlap_percent=10)

    chunks = chunk_blocks(paragraphs("a", 159), config)

    # 100 + 59 new words cannot be merged (159 > 120): the two chunks share the words, 10 of them in common.
    assert [c.word_count for c in chunks] == [85, 84]
    assert shared_prefix_suffix(chunks[0], chunks[1]) == 10
    assert join_without_overlap(chunks) == words("a", 159)


def test_AC_021_a_remainder_is_merged_when_the_result_fits_in_the_maximum() -> None:
    config = ChunkerConfig(target_words=100, min_words=60, max_words=120, overlap_percent=10)

    chunks = chunk_blocks(paragraphs("a", 110), config)

    assert [c.word_count for c in chunks] == [110]


def test_AC_021_a_section_exactly_at_the_target_is_one_chunk() -> None:
    chunks = chunk_blocks(paragraphs("a", 300), DEFAULTS)

    assert [c.word_count for c in chunks] == [300]


def test_AC_021_a_section_of_one_word_is_one_chunk() -> None:
    chunks = chunk_blocks([heading("Solo"), Block("paragraph", "parola")], DEFAULTS)

    assert [(c.section_title, c.content, c.word_count) for c in chunks] == [("Solo", "parola", 1)]


def test_AC_021_paragraph_breaks_and_line_breaks_are_kept_inside_a_chunk() -> None:
    blocks = [Block("paragraph", "uno due\ntre"), Block("paragraph", "quattro  cinque")]

    chunks = chunk_blocks(blocks, DEFAULTS)

    assert [c.content for c in chunks] == ["uno due\ntre\n\nquattro  cinque"]
    assert chunks[0].word_count == 5


# ---------------------------------------------------------------------------------------------------------------------
# Section titles (AC-018 through the chunker) and sections
# ---------------------------------------------------------------------------------------------------------------------


def test_AC_018_section_title_is_the_path_of_the_open_headings() -> None:
    blocks = [
        Block("paragraph", "prima di tutto"),
        heading("A", 1),
        Block("paragraph", "testo a"),
        heading("B", 2),
        Block("paragraph", "testo b"),
        heading("C", 3),
        Block("paragraph", "testo c"),
        heading("D", 2),
        Block("paragraph", "testo d"),
        heading("E", 1),
        Block("paragraph", "testo e"),
    ]

    chunks = chunk_blocks(blocks, DEFAULTS)

    assert [(c.section_title, c.content) for c in chunks] == [
        (None, "prima di tutto"),
        ("A", "testo a"),
        ("A > B", "testo b"),
        ("A > B > C", "testo c"),
        ("A > D", "testo d"),
        ("E", "testo e"),
    ]


def test_AC_018_a_heading_that_skips_levels_is_appended_to_the_open_ones() -> None:
    blocks = [heading("A", 1), heading("C", 3), Block("paragraph", "testo")]

    assert [c.section_title for c in chunk_blocks(blocks, DEFAULTS)] == ["A > C"]


def test_AC_018_a_heading_closes_the_open_headings_of_the_same_or_deeper_level() -> None:
    blocks = [heading("A", 2), Block("paragraph", "uno"), heading("B", 1), Block("paragraph", "due")]

    assert [c.section_title for c in chunk_blocks(blocks, DEFAULTS)] == ["A", "B"]


def test_AC_018_the_heading_text_is_not_part_of_the_content_and_empty_sections_give_no_chunk() -> None:
    blocks = [heading("Vuota"), heading("Piena", 2), Block("paragraph", "contenuto"), heading("Ultima")]

    chunks = chunk_blocks(blocks, DEFAULTS)

    assert [(c.section_title, c.content) for c in chunks] == [("Vuota > Piena", "contenuto")]


def test_AC_018_two_sections_with_the_same_title_are_never_merged() -> None:
    blocks = [heading("Note"), Block("paragraph", "uno"), heading("Note"), Block("paragraph", "due")]

    chunks = chunk_blocks(blocks, DEFAULTS)

    assert [(c.section_title, c.content) for c in chunks] == [("Note", "uno"), ("Note", "due")]


def test_AC_018_text_before_the_first_heading_has_no_section_title() -> None:
    chunks = chunk_blocks([Block("paragraph", "preambolo"), heading("Titolo"), Block("paragraph", "testo")], DEFAULTS)

    assert [c.section_title for c in chunks] == [None, "Titolo"]


# ---------------------------------------------------------------------------------------------------------------------
# AC-022: tables
# ---------------------------------------------------------------------------------------------------------------------


def table_rows(count: int, start: int = 0) -> list[Block]:
    """`count` rows of 18 to 22 words each, with unique words."""
    rows: list[Block] = []
    cursor = 0
    for i in range(count):
        size = 18 + (start + i) % 5
        rows.append(Block("table_row", " ".join(words("r", size, cursor))))
        cursor += size
    return rows


def split_rows(chunk: ChunkDraft) -> list[str]:
    return chunk.content.split("\n")


def test_AC_022_every_chunk_starts_with_a_whole_row_and_no_row_is_split() -> None:
    rows = table_rows(30)
    row_texts = [r.text for r in rows]

    chunks = chunk_blocks([heading("Tabella"), *rows], DEFAULTS)

    assert len(chunks) >= 2
    covered: list[int] = []
    for chunk in chunks:
        lines = split_rows(chunk)
        first = row_texts.index(lines[0])
        assert lines == row_texts[first : first + len(lines)]
        covered.extend(range(first, first + len(lines)))
        assert chunk.word_count <= 450
    assert covered[0] == 0
    assert covered[-1] == 29
    assert set(covered) == set(range(30))


def test_AC_022_consecutive_table_chunks_overlap_by_the_fewest_whole_rows_with_at_least_45_words() -> None:
    rows = table_rows(30)
    row_texts = [r.text for r in rows]

    chunks = chunk_blocks(rows, DEFAULTS)

    assert len(chunks) >= 2
    for previous, current in zip(chunks, chunks[1:], strict=False):
        previous_rows, current_rows = split_rows(previous), split_rows(current)
        start = row_texts.index(current_rows[0])
        overlap_rows = previous_rows[start - row_texts.index(previous_rows[0]) :]
        overlap_words = sum(len(r.split()) for r in overlap_rows)
        assert overlap_rows == current_rows[: len(overlap_rows)]
        assert overlap_words >= 45
        assert overlap_words - len(overlap_rows[0].split()) < 45


def test_AC_022_a_table_row_longer_than_the_maximum_is_a_chunk_by_itself_without_overlap() -> None:
    before = table_rows(5)
    long_row = Block("table_row", " ".join(words("L", 500)))
    after = [Block("table_row", " ".join(words(f"z{i}x", 20))) for i in range(5)]

    chunks = chunk_blocks([*before, long_row, *after], DEFAULTS)

    assert [c.content for c in chunks] == [
        "\n".join(r.text for r in before),
        long_row.text,
        "\n".join(r.text for r in after),
    ]
    assert chunks[1].word_count == 500


def test_AC_022_a_remainder_after_a_row_longer_than_the_maximum_stays_a_chunk_of_its_own() -> None:
    long_row = Block("table_row", " ".join(words("L", 500)))

    chunks = chunk_blocks([long_row, Block("paragraph", "coda di otto parole " * 2)], DEFAULTS)

    assert [c.word_count for c in chunks] == [500, 8]


def test_AC_022_prose_chunks_overlap_exactly_and_chunks_ending_in_a_table_overlap_by_whole_rows() -> None:
    prose = Block("paragraph", " ".join(words("p", 400)))
    rows = [Block("table_row", " ".join(words(f"t{i}x", 20))) for i in range(10)]

    chunks = chunk_blocks([prose, *rows], DEFAULTS)

    assert [c.word_count for c in chunks] == [300, 285, 120]
    assert chunk_words(chunks[0])[-45:] == chunk_words(chunks[1])[:45]
    # the second chunk ends with a whole row and the third starts at a row: overlap of 3 rows (60 words)
    assert chunks[1].content.endswith(rows[6].text)
    assert chunks[2].content.startswith(rows[4].text)
    assert split_rows(chunks[2])[-1] == rows[9].text


# ---------------------------------------------------------------------------------------------------------------------
# AC-023: position, pages, whole text
# ---------------------------------------------------------------------------------------------------------------------


def test_AC_023_position_runs_from_zero_to_n_minus_one_in_reading_order() -> None:
    chunks = chunk_blocks(three_sections(), DEFAULTS)

    assert [c.position for c in chunks] == list(range(len(chunks)))
    assert [c.section_title for c in chunks] == ["Uno", "Uno", "Uno", "Due", "Tre"]


def test_AC_023_joining_the_chunks_without_the_overlap_gives_the_whole_normalized_text() -> None:
    blocks = [
        Block("paragraph", " ".join(words("p", 120))),
        *three_sections(),
        heading("Tabella", 2),
        *table_rows(30),
    ]
    expected = [w for b in blocks if b.kind != "heading" for w in b.text.split()]

    chunks = chunk_blocks(blocks, DEFAULTS)

    assert join_without_overlap(chunks) == expected


def test_AC_023_page_start_and_page_end_are_the_smallest_and_largest_page_of_the_blocks() -> None:
    paras = paragraphs("a", 700)
    blocks = [replace_page(b, i // 2 + 1) for i, b in enumerate(paras)]

    chunks = chunk_blocks(blocks, DEFAULTS)

    assert [(c.page_start, c.page_end) for c in chunks] == [(1, 2), (2, 3), (3, 4)]
    assert all(c.page_start is not None and c.page_end is not None and c.page_start <= c.page_end for c in chunks)


def test_AC_023_pages_are_none_when_no_block_has_a_page() -> None:
    chunks = chunk_blocks(paragraphs("a", 700), DEFAULTS)

    assert [(c.page_start, c.page_end) for c in chunks] == [(None, None)] * 3


def test_AC_023_unknown_pages_are_ignored_when_some_block_has_a_page() -> None:
    blocks = [
        Block("paragraph", "uno due", page=None),
        Block("paragraph", "tre", page=5),
        Block("paragraph", "quattro"),
    ]

    chunks = chunk_blocks(blocks, DEFAULTS)

    assert [(c.page_start, c.page_end) for c in chunks] == [(5, 5)]


def test_AC_023_each_section_has_its_own_pages() -> None:
    blocks = [
        heading("Uno"),
        Block("paragraph", "alfa", page=1),
        heading("Due"),
        Block("paragraph", "beta", page=2),
        Block("paragraph", "gamma", page=3),
    ]

    chunks = chunk_blocks(blocks, DEFAULTS)

    assert [(c.section_title, c.page_start, c.page_end) for c in chunks] == [("Uno", 1, 1), ("Due", 2, 3)]


def test_AC_023_word_count_is_the_number_of_words_of_the_content() -> None:
    chunks = chunk_blocks([*three_sections(), *table_rows(10)], DEFAULTS)

    assert all(c.word_count == len(c.content.split()) for c in chunks)


# ---------------------------------------------------------------------------------------------------------------------
# Empty and odd inputs
# ---------------------------------------------------------------------------------------------------------------------


def test_AC_023_no_blocks_give_no_chunks() -> None:
    assert chunk_blocks([], DEFAULTS) == []


def test_AC_023_headings_alone_and_whitespace_only_blocks_give_no_chunks() -> None:
    blocks = [heading("Solo titolo"), Block("paragraph", "  \n\t "), Block("table_row", "")]

    assert chunk_blocks(blocks, DEFAULTS) == []


def test_AC_023_a_single_word_of_100000_characters_is_one_chunk_of_one_word() -> None:
    long_word = "y" * 100_000

    chunks = chunk_blocks([Block("paragraph", long_word)], DEFAULTS)

    assert [(c.content, c.word_count) for c in chunks] == [(long_word, 1)]
