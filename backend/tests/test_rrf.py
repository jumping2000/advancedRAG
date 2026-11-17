# SPEC-2026-11-17-fase-3-chat
"""Reciprocal Rank Fusion of the two candidate lists: AC-009 (REQ-004).

Pure function, no database. The expected scores are derived from the formula of the spec, score = sum of 1 / (k + rank)
over the lists a chunk is in, with rank from 1; they are compared exactly (as the float of the exact fraction), and the
tie-breaks are the ones of REQ-004: better dense rank first, a chunk that is not in the dense list after the ones that
are.
"""

from __future__ import annotations

import dataclasses
import uuid
from fractions import Fraction

import pytest

from app.chat.retrieval import Ranked, fuse


def ids(*numbers: int) -> list[uuid.UUID]:
    return [uuid.UUID(int=number) for number in numbers]


A, B, C, D = ids(1, 2, 3, 4)


def exact(*terms: Fraction) -> float:
    return float(sum(terms, Fraction(0)))


def test_AC_009_fuses_the_two_lists_in_the_order_of_the_spec() -> None:
    """Dense [a, b, c] and BM25 [c, d, a] with k = 60 give a, c, b, d."""
    fused = fuse([A, B, C], [C, D, A], 60)

    assert [r.chunk_id for r in fused] == [A, C, B, D]


def test_AC_009_scores_and_ranks_of_each_chunk() -> None:
    """a: 1/61 + 1/63, c: 1/63 + 1/61, b: 1/62, d: 1/62; every chunk once with its rank in each list (or None)."""
    fused = fuse([A, B, C], [C, D, A], 60)

    assert fused == [
        Ranked(A, 1, 3, exact(Fraction(1, 61), Fraction(1, 63))),
        Ranked(C, 3, 1, exact(Fraction(1, 63), Fraction(1, 61))),
        Ranked(B, 2, None, exact(Fraction(1, 62))),
        Ranked(D, None, 2, exact(Fraction(1, 62))),
    ]
    assert fused[0].score == fused[1].score  # the tie that the dense rank resolves (a before c)
    assert fused[2].score == fused[3].score  # and the one between b and d (b before d)


def test_AC_009_each_chunk_appears_once() -> None:
    fused = fuse([A, B, C], [C, D, A], 60)

    assert len({r.chunk_id for r in fused}) == len(fused) == 4


def test_AC_009_the_exact_fractions_decide_ties_that_floats_cannot_tell_apart() -> None:
    """With a huge k, 1/(k+1) + 1/(k+3) and 2/(k+2) are the same float but not the same number: the first is larger
    (the product (k+1)(k+3) is smaller than (k+2)^2), so its chunk wins although its dense rank is worse."""
    k = 10**9
    p, loser, winner, q = ids(10, 11, 12, 13)
    # winner: dense 3 and BM25 1; loser: dense 2 and BM25 2; p only dense 1; q only BM25 3
    fused = fuse([p, loser, winner], [winner, loser, q], k)

    assert [r.chunk_id for r in fused] == [winner, loser, p, q]
    assert 1 / (k + 1) + 1 / (k + 3) == 2 / (k + 2)  # a float comparison would have said "tie"


def test_AC_009_a_chunk_not_in_the_dense_list_sorts_after_the_ones_in_it_at_equal_score() -> None:
    """Dense [a] and BM25 [b]: both score 1/61; a has a dense rank, b has none."""
    assert [r.chunk_id for r in fuse([A], [B], 60)] == [A, B]
    # the order of the arguments, not of the ids, decides: b is the dense one here, and a the BM25 one
    assert [r.chunk_id for r in fuse([B], [A], 60)] == [B, A]


def test_AC_009_equal_score_and_both_in_the_dense_list_the_better_dense_rank_wins() -> None:
    """Dense [b, a] and BM25 [a, b]: both 1/61 + 1/62, b has dense rank 1 although its id is the larger one."""
    b, a = ids(2, 1)
    fused = fuse([b, a], [a, b], 60)

    assert [r.chunk_id for r in fused] == [b, a]
    assert fused[0].score == fused[1].score


def test_AC_009_a_chunk_only_in_one_list() -> None:
    assert fuse([A, B], [], 60) == [
        Ranked(A, 1, None, exact(Fraction(1, 61))),
        Ranked(B, 2, None, exact(Fraction(1, 62))),
    ]
    assert fuse([], [A, B], 60) == [
        Ranked(A, None, 1, exact(Fraction(1, 61))),
        Ranked(B, None, 2, exact(Fraction(1, 62))),
    ]


def test_AC_009_empty_lists() -> None:
    assert fuse([], [], 60) == []


def test_AC_009_a_duplicate_inside_a_list_is_ignored_after_its_first_rank() -> None:
    fused = fuse([A, B, A], [], 60)

    assert fused == [Ranked(A, 1, None, exact(Fraction(1, 61))), Ranked(B, 2, None, exact(Fraction(1, 62)))]
    assert fuse([], [A, A], 60) == [Ranked(A, None, 1, exact(Fraction(1, 61)))]


def test_AC_009_the_k_parameter_changes_the_scores() -> None:
    assert fuse([A], [A], 1) == [Ranked(A, 1, 1, exact(Fraction(1, 2), Fraction(1, 2)))]
    assert fuse([A], [], 10) == [Ranked(A, 1, None, exact(Fraction(1, 11)))]
    assert fuse([A], [], 60)[0].score != fuse([A], [], 10)[0].score


@pytest.mark.parametrize("k", [0, -1, -60])
def test_AC_009_k_below_one_is_refused(k: int) -> None:
    """RRF_K is at least 1 (plan); a k that would divide by zero is a programming error, not a result."""
    with pytest.raises(ValueError):
        fuse([A], [B], k)


def test_AC_009_ranked_is_a_frozen_value() -> None:
    ranked = fuse([A], [A], 60)[0]

    assert [field.name for field in dataclasses.fields(Ranked)] == ["chunk_id", "dense_rank", "bm25_rank", "score"]
    with pytest.raises(dataclasses.FrozenInstanceError):
        ranked.score = 0.0  # type: ignore[misc]


def test_AC_009_the_inputs_are_not_modified() -> None:
    dense, bm25 = [A, B, C], [C, D, A]
    fuse(dense, bm25, 60)

    assert dense == [A, B, C]
    assert bm25 == [C, D, A]


def test_AC_009_the_score_is_a_float_and_the_ranks_are_ints() -> None:
    for ranked in fuse([A, B, C], [C, D, A], 60):
        assert type(ranked.score) is float
        assert ranked.dense_rank is None or type(ranked.dense_rank) is int
        assert ranked.bm25_rank is None or type(ranked.bm25_rank) is int
