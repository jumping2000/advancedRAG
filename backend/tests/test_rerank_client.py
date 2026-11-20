# SPEC-2026-11-20-fase-4-rerank-rewrite
"""The client of the reranker and the final order (T-03): the requests of AC-014 (`tei`) and AC-015 (`cohere`), the
failures of AC-017 (REQ-008, REQ-009), against `fake_reranker` (real local sockets, never a real service), and the pure
`rerank_order`.

`RerankClient.scores(query, texts)` gives one score per text, in the order of the texts, or raises `RerankError` with a
reason in `unreachable`, `http_error`, `timeout`, `invalid_response`. The error holds a fixed message (never the query,
a text, the body received or the key) and no other exception. Review focus of T-03: a score NaN, Infinity, a boolean or
a numeric string is `invalid_response` (the context keeps the RRF order); a chunk longer than the limit of the model is
sent with `truncate: true` in the `tei` format, so the reranker shortens it instead of refusing it (413).
"""

from __future__ import annotations

import time
import uuid
from typing import Any

import pytest

from app.chat.rerank import RerankClient, RerankError, rerank_order
from app.chat.retrieval import Candidate
from tests.fakes.http_server import closed_port_url
from tests.fakes.services import FakeReranker, fake_reranker

QUERY = "Quanto costa la spedizione del prodotto X? ☃"
TEXTS = ["Paragrafo alfa del listino", "Paragrafo beta della garanzia", "Paragrafo gamma della spedizione"]
SCORES = {"alfa": 0.5, "beta": 0.1, "gamma": 0.9}
MODEL = "modello-del-reranker"
KEY = "sk-chiave-reranker-t03"
APIS = ["tei", "cohere"]


def make_client(
    reranker: FakeReranker | str, api: Any = "tei", api_key: str | None = None, timeout: float = 10.0
) -> RerankClient:
    if isinstance(reranker, str):
        return RerankClient(api, reranker, MODEL, api_key, timeout)
    return RerankClient(reranker.api, reranker.url, MODEL, api_key, timeout)  # type: ignore[arg-type]


async def failure_of(client: RerankClient, texts: list[str] | None = None) -> RerankError:
    with pytest.raises(RerankError) as raised:
        await client.scores(QUERY, TEXTS if texts is None else texts)
    return raised.value


def assert_clean(error: RerankError, *forbidden: str) -> None:
    """A fixed message, nothing of the request or of the answer, no other exception attached."""
    text = f"{error!s} {error!r}"
    for secret in (QUERY, KEY, "failure on purpose", *TEXTS, *forbidden):
        assert secret not in text
    assert error.__cause__ is None
    assert error.__context__ is None


# AC-014: the `tei` format ---------------------------------------------------------------------------------------------


async def test_AC_014_tei_posts_query_texts_and_truncate_true_to_rerank_without_a_key() -> None:
    with fake_reranker("tei", scores=SCORES) as reranker:
        scores = await make_client(reranker).scores(QUERY, TEXTS)

    assert scores == [0.5, 0.1, 0.9]  # in the order of the texts, not the order of the answer (highest first)
    (request,) = reranker.requests
    assert (request.method, request.path) == ("POST", "/rerank")
    assert request.authorization is None
    assert request.body == {"query": QUERY, "texts": TEXTS, "truncate": True}


async def test_AC_014_tei_sends_the_key_as_bearer_when_it_is_set() -> None:
    with fake_reranker("tei", scores=SCORES) as reranker:
        await make_client(reranker, api_key=KEY).scores(QUERY, TEXTS)

    assert reranker.requests[0].authorization == f"Bearer {KEY}"


@pytest.mark.parametrize("key", [None, "", "   "])
@pytest.mark.parametrize("api", APIS)
async def test_without_a_key_there_is_no_authorization_header(api: str, key: str | None) -> None:
    with fake_reranker(api, scores=SCORES) as reranker:
        await make_client(reranker, api_key=key).scores(QUERY, TEXTS)

    assert reranker.requests[0].authorization is None


async def test_AC_014_tei_reads_the_scores_by_index_whatever_the_order_of_the_answer() -> None:
    results = [{"index": 2, "score": 0.25}, {"index": 0, "score": 0.75}, {"index": 1, "score": -3.5}]
    with fake_reranker("tei", results=results) as reranker:
        assert await make_client(reranker).scores(QUERY, TEXTS) == [0.75, -3.5, 0.25]


async def test_tei_ignores_the_other_fields_of_an_item() -> None:
    results = [{"index": 1, "score": 0.2, "text": "x"}, {"index": 0, "score": 0.1, "text": "y"}]
    with fake_reranker("tei", results=results) as reranker:
        assert await make_client(reranker).scores(QUERY, TEXTS[:2]) == [0.1, 0.2]


async def test_an_integer_score_is_a_number() -> None:
    results = [{"index": 0, "score": 1}, {"index": 1, "score": 0}, {"index": 2, "score": -2}]
    with fake_reranker("tei", results=results) as reranker:
        assert await make_client(reranker).scores(QUERY, TEXTS) == [1.0, 0.0, -2.0]


async def test_review_focus_a_text_longer_than_the_limit_of_the_model_is_sent_with_truncate_and_scored() -> None:
    """The fake refuses with 413, as text-embeddings-inference does, a text over its limit without `truncate: true`."""
    long_text = "Paragrafo gamma " + "molto lungo " * 4000
    texts = [TEXTS[0], long_text]
    with fake_reranker("tei", scores=SCORES, max_text_chars=512) as reranker:
        assert await make_client(reranker).scores(QUERY, texts) == [0.5, 0.9]

    assert reranker.requests[0].body["truncate"] is True
    assert reranker.requests[0].body["texts"] == texts  # sent whole: the reranker shortens it


@pytest.mark.parametrize("api", APIS)
async def test_a_trailing_slash_in_the_base_url_is_ignored(api: str) -> None:
    with fake_reranker(api, scores=SCORES) as reranker:
        await make_client(reranker.url + "/", api=api).scores(QUERY, TEXTS)

    assert reranker.requests[0].path == f"{reranker.prefix}/rerank"


@pytest.mark.parametrize("api", APIS)
async def test_no_text_gives_no_score_and_sends_nothing(api: str) -> None:
    with fake_reranker(api) as reranker:
        assert await make_client(reranker).scores(QUERY, []) == []

    assert reranker.requests == []


# AC-015: the `cohere` format ------------------------------------------------------------------------------------------


async def test_AC_015_cohere_posts_model_query_documents_and_top_n_with_the_key() -> None:
    with fake_reranker("cohere", scores=SCORES) as reranker:
        scores = await make_client(reranker, api_key=KEY).scores(QUERY, TEXTS)

    assert scores == [0.5, 0.1, 0.9]
    (request,) = reranker.requests
    assert (request.method, request.path) == ("POST", "/v1/rerank")
    assert request.authorization == f"Bearer {KEY}"
    assert request.body == {"model": MODEL, "query": QUERY, "documents": TEXTS, "top_n": 3}


async def test_AC_015_cohere_reads_the_relevance_scores_by_index_whatever_the_order_of_the_answer() -> None:
    results = [
        {"index": 1, "relevance_score": 0.9},
        {"index": 2, "relevance_score": 0.3},
        {"index": 0, "relevance_score": 0.1},
    ]
    with fake_reranker("cohere", results=results) as reranker:
        assert await make_client(reranker).scores(QUERY, TEXTS) == [0.1, 0.9, 0.3]


async def test_cohere_ignores_the_other_fields_of_the_answer_and_of_an_item() -> None:
    body = {
        "id": "x",
        "results": [
            {"index": 1, "relevance_score": 0.2, "document": {"text": "y"}},
            {"index": 0, "relevance_score": 0.4},
        ],
        "meta": {"billed_units": {"search_units": 1}},
    }
    with fake_reranker("cohere", json_body=body) as reranker:
        assert await make_client(reranker).scores(QUERY, TEXTS[:2]) == [0.4, 0.2]


# AC-017: the failures -------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("api", APIS)
async def test_AC_017_a_reranker_that_cannot_be_reached_is_unreachable(api: str) -> None:
    error = await failure_of(make_client(closed_port_url(), api=api, api_key=KEY))

    assert error.reason == "unreachable"
    assert_clean(error)


@pytest.mark.parametrize("api", APIS)
async def test_AC_017_a_connection_closed_without_an_answer_is_unreachable(api: str) -> None:
    with fake_reranker(api, drop_connection=True) as reranker:
        error = await failure_of(make_client(reranker, api_key=KEY))

    assert error.reason == "unreachable"
    assert_clean(error)


@pytest.mark.parametrize("status", [400, 401, 404, 413, 422, 429, 500, 503])
@pytest.mark.parametrize("api", APIS)
async def test_AC_017_a_status_that_is_not_2xx_is_http_error(api: str, status: int) -> None:
    with fake_reranker(api, fail_status=status) as reranker:
        error = await failure_of(make_client(reranker, api_key=KEY))

    assert error.reason == "http_error"
    assert_clean(error)


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
async def test_a_redirect_is_http_error_and_nothing_is_sent_to_its_target(status: int) -> None:
    with fake_reranker("tei", scores=SCORES) as target:
        with fake_reranker("tei", redirect_to=f"{target.url}/rerank", redirect_status=status) as reranker:
            error = await failure_of(make_client(reranker, api_key=KEY))

    assert error.reason == "http_error"
    assert len(reranker.requests) == 1
    assert target.requests == []  # neither the texts nor the key went to the target of the redirect


@pytest.mark.parametrize("api", APIS)
async def test_AC_017_a_reranker_slower_than_the_timeout_is_timeout_after_the_timeout(api: str) -> None:
    with fake_reranker(api, scores=SCORES, delay_seconds=5) as reranker:
        started = time.monotonic()
        error = await failure_of(make_client(reranker, timeout=1))
        elapsed = time.monotonic() - started

    assert error.reason == "timeout"
    assert 0.9 <= elapsed < 3
    assert_clean(error)


async def test_the_timeout_covers_the_body_too_not_only_the_headers() -> None:
    """One deadline for the whole request: headers that arrive at once and a body that does not are a timeout."""
    with fake_reranker("tei", scores=SCORES, body_delay_seconds=5) as reranker:
        started = time.monotonic()
        error = await failure_of(make_client(reranker, timeout=1))
        elapsed = time.monotonic() - started

    assert error.reason == "timeout"
    assert elapsed < 3


async def test_an_answer_within_the_timeout_is_not_cut() -> None:
    with fake_reranker("tei", scores=SCORES, delay_seconds=0.3) as reranker:
        assert await make_client(reranker, timeout=2).scores(QUERY, TEXTS) == [0.5, 0.1, 0.9]


def tei(*items: tuple[Any, Any]) -> list[dict[str, Any]]:
    return [{"index": index, "score": score} for index, score in items]


def cohere(*items: tuple[Any, Any]) -> list[dict[str, Any]]:
    return [{"index": index, "relevance_score": score} for index, score in items]


INVALID_ITEMS: dict[str, tuple[tuple[Any, Any], ...]] = {
    # (index, score) of the answer to the three TEXTS
    "an index out of the candidates": ((0, 0.5), (1, 0.1), (3, 0.9)),
    "a negative index": ((0, 0.5), (1, 0.1), (-1, 0.9)),
    "a repeated index": ((0, 0.5), (1, 0.1), (1, 0.9)),
    "a missing candidate": ((0, 0.5), (2, 0.9)),
    "an extra item": ((0, 0.5), (1, 0.1), (2, 0.9), (3, 0.2)),
    "a score NaN": ((0, 0.5), (1, float("nan")), (2, 0.9)),
    "a score Infinity": ((0, 0.5), (1, float("inf")), (2, 0.9)),
    "a score -Infinity": ((0, float("-inf")), (1, 0.1), (2, 0.9)),
    "a score true": ((0, 0.5), (1, True), (2, 0.9)),
    "a score false": ((0, False), (1, 0.1), (2, 0.9)),
    "a numeric string as score": ((0, 0.5), (1, "0.1"), (2, 0.9)),
    "a score null": ((0, 0.5), (1, None), (2, 0.9)),
    "a score that is a list": ((0, 0.5), (1, [0.1]), (2, 0.9)),
    "an index true": ((0, 0.5), (True, 0.1), (2, 0.9)),
    "an index 1.0": ((0, 0.5), (1.0, 0.1), (2, 0.9)),
    "a numeric string as index": ((0, 0.5), ("1", 0.1), (2, 0.9)),
    "an index null": ((0, 0.5), (None, 0.1), (2, 0.9)),
}


@pytest.mark.parametrize("case", list(INVALID_ITEMS))
@pytest.mark.parametrize("api", APIS)
async def test_AC_017_an_invalid_index_or_score_is_invalid_response(api: str, case: str) -> None:
    """Review focus: NaN, Infinity, booleans and numeric strings are not scores."""
    items = (cohere if api == "cohere" else tei)(*INVALID_ITEMS[case])
    with fake_reranker(api, results=items) as reranker:
        error = await failure_of(make_client(reranker, api_key=KEY))

    assert error.reason == "invalid_response"
    assert_clean(error)


@pytest.mark.parametrize("api", APIS)
async def test_a_score_too_large_for_a_float_is_invalid_response(api: str) -> None:
    name = "relevance_score" if api == "cohere" else "score"
    items = f'[{{"index": 0, "{name}": 1e400}}, {{"index": 1, "{name}": 0.1}}, {{"index": 2, "{name}": 0.9}}]'
    body = f'{{"results": {items}}}' if api == "cohere" else items
    with fake_reranker(api, raw_body=body.encode()) as reranker:
        error = await failure_of(make_client(reranker))

    assert error.reason == "invalid_response"


@pytest.mark.parametrize("api", APIS)
async def test_an_item_without_its_score_or_index_is_invalid_response(api: str) -> None:
    name = "relevance_score" if api == "cohere" else "score"
    for items in (
        [{"index": 0, name: 0.5}, {"index": 1}, {"index": 2, name: 0.9}],
        [{"index": 0, name: 0.5}, {name: 0.1}, {"index": 2, name: 0.9}],
        [{"index": 0, name: 0.5}, "x", {"index": 2, name: 0.9}],
    ):
        with fake_reranker(api, results=items) as reranker:
            error = await failure_of(make_client(reranker))
        assert error.reason == "invalid_response", items


async def test_the_score_field_of_the_other_format_is_not_read() -> None:
    with fake_reranker("tei", results=cohere((0, 0.5), (1, 0.1), (2, 0.9))) as reranker:
        assert (await failure_of(make_client(reranker))).reason == "invalid_response"
    with fake_reranker("cohere", results=tei((0, 0.5), (1, 0.1), (2, 0.9))) as reranker:
        assert (await failure_of(make_client(reranker))).reason == "invalid_response"


TEI_BODIES = [
    b"not json at all",
    b"[{",
    b"\xff\xfe\x00",
    b'"a string"',
    b"42",
    b"null",
    b"{}",
    b'{"results": [{"index": 0, "score": 0.5}]}',  # the envelope of the other format
    b'[["0", 0.5]]',
    b'{"error": "failure on purpose"}',
]
COHERE_BODIES = [
    b"not json at all",
    b'{"results": [',
    b"\xff\xfe\x00",
    b'"a string"',
    b"[]",
    b"{}",
    b'{"results": null}',
    b'{"results": "x"}',
    b'{"results": {"index": 0}}',
    b'[{"index": 0, "relevance_score": 0.5}]',  # the list of the other format
    b'{"error": "failure on purpose"}',
]


@pytest.mark.parametrize(("api", "body"), [("tei", b) for b in TEI_BODIES] + [("cohere", b) for b in COHERE_BODIES])
async def test_a_body_of_the_wrong_shape_is_invalid_response(api: str, body: bytes) -> None:
    with fake_reranker(api, raw_body=body) as reranker:
        error = await failure_of(make_client(reranker, api_key=KEY), texts=TEXTS[:1])

    assert error.reason == "invalid_response"
    assert_clean(error)


@pytest.mark.parametrize("api", APIS)
async def test_an_empty_answer_to_texts_is_invalid_response(api: str) -> None:
    with fake_reranker(api, results=[]) as reranker:
        error = await failure_of(make_client(reranker))

    assert error.reason == "invalid_response"


@pytest.mark.parametrize("api", APIS)
async def test_a_huge_body_is_invalid_response(api: str) -> None:
    name = "relevance_score" if api == "cohere" else "score"
    items: list[Any] = [{"index": i, name: 0.1, "padding": "p" * (2 * 1024 * 1024)} for i in range(3)]
    with fake_reranker(api, results=items) as reranker:
        error = await failure_of(make_client(reranker))

    assert error.reason == "invalid_response"


@pytest.mark.parametrize("api", APIS)
async def test_a_key_with_a_non_ascii_character_is_unreachable_without_leaking_it(api: str) -> None:
    key = "sk-chiavè-segreta-reranker"
    with fake_reranker(api, scores=SCORES) as reranker:
        error = await failure_of(make_client(reranker, api_key=key))

    assert error.reason == "unreachable"
    assert_clean(error, key)


async def test_a_base_url_that_is_not_a_url_is_unreachable() -> None:
    error = await failure_of(make_client("not a url"))

    assert error.reason == "unreachable"


@pytest.mark.parametrize("reason", ["unreachable", "http_error", "timeout", "invalid_response"])
def test_rerank_error_carries_its_reason_and_a_fixed_message(reason: Any) -> None:
    error = RerankError(reason)

    assert error.reason == reason
    assert str(error)


@pytest.mark.parametrize("api", APIS)
def test_the_key_is_not_shown_by_the_client_or_its_attributes(api: Any) -> None:
    client = RerankClient(api, "http://127.0.0.1:9", MODEL, KEY, 5)

    assert KEY not in repr(client)
    assert KEY not in repr(vars(client))


# rerank_order: the final order --------------------------------------------------------------------------------------


def candidates(*names: str) -> list[Candidate]:
    return [
        Candidate(
            chunk_id=uuid.uuid4(),
            document_id=uuid.uuid4(),
            document_name="manuale.pdf",
            section_title=None,
            page_start=None,
            page_end=None,
            content=name,
            dense_rank=rank,
            bm25_rank=None,
            score=1 / (60 + rank),
        )
        for rank, name in enumerate(names, start=1)
    ]


def names(ordered: list[Candidate]) -> list[str]:
    return [candidate.content for candidate in ordered]


def test_AC_014_the_scored_candidates_by_score_then_the_others_in_rrf_order() -> None:
    fused = candidates("a", "b", "c", "d", "e")

    ordered = rerank_order(fused, [0.5, 0.1, 0.9])

    assert names(ordered) == ["c", "a", "b", "d", "e"]
    assert ordered[0] is fused[2]  # the same candidates, with their RRF ranks and score
    assert [c.score for c in ordered] == [
        fused[2].score,
        fused[0].score,
        fused[1].score,
        fused[3].score,
        fused[4].score,
    ]


def test_equal_scores_keep_the_rrf_order() -> None:
    fused = candidates("a", "b", "c", "d")

    assert names(rerank_order(fused, [0.2, 0.7, 0.2, 0.7])) == ["b", "d", "a", "c"]
    assert names(rerank_order(fused, [0.0, 0.0, 0.0])) == ["a", "b", "c", "d"]


def test_negative_scores_and_every_candidate_scored() -> None:
    fused = candidates("a", "b", "c")

    assert names(rerank_order(fused, [-4.0, 2.5, -0.5])) == ["b", "c", "a"]


def test_no_score_keeps_the_rrf_order() -> None:
    fused = candidates("a", "b")

    assert names(rerank_order(fused, [])) == ["a", "b"]


def test_the_input_is_not_changed() -> None:
    fused = candidates("a", "b", "c")
    before = list(fused)

    rerank_order(fused, [0.1, 0.2, 0.3])

    assert fused == before


def test_more_scores_than_candidates_is_an_error() -> None:
    with pytest.raises(ValueError):
        rerank_order(candidates("a"), [0.1, 0.2])
