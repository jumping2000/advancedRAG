# SPEC-2026-11-20-fase-4-rerank-rewrite
"""The client of the rewriting service (T-02): the request of AC-009 (instructions, history, question) and the
failures of AC-011 (REQ-005, REQ-006), against `fake_rewrite_provider` (real local sockets, never a real service).

`QuestionRewriter.rewrite` posts `{base}/chat/completions` with `stream: false` and gives the text of
`choices[0].message.content` without the spaces at both ends, or raises `RewriteError` with a reason in `unreachable`,
`http_error`, `timeout`, `invalid_response`. The error holds a fixed message (never the question, the history, the body
received or the key) and no other exception. Review focus of T-02: a text over several lines, with quotes or a preamble
is given as it is; a text that the database could not hold (a NUL, a lone surrogate) is not usable (the checks of a
question of phase 3), so the search falls back to the original question.
"""

from __future__ import annotations

import time
from typing import Any

import pytest

from app.chat.rewrite import REWRITE_INSTRUCTIONS, QuestionRewriter, RewriteError, build_rewrite_messages
from app.db.models import Message
from tests.fakes.http_server import closed_port_url
from tests.fakes.services import FakeRewriteProvider, completion_body, fake_rewrite_provider

QUESTION = "e la spedizione? ☃"
REWRITTEN = "Quanto costa la spedizione del prodotto X?"
MODEL = "modello-di-riscrittura"
KEY = "sk-chiave-riscrittura-t02"
MAX_CHARS = 4000


def turn(role: str, content: str) -> Message:
    return Message(role=role, content=content, status="COMPLETED")


HISTORY = [turn("user", "Quanto costa il prodotto X?"), turn("assistant", "Costa 10 euro [1].")]


def make_rewriter(
    provider: FakeRewriteProvider | str, api_key: str | None = None, timeout: float = 10.0, max_chars: int = MAX_CHARS
) -> QuestionRewriter:
    url = provider if isinstance(provider, str) else provider.url
    return QuestionRewriter(url, MODEL, api_key, timeout, max_chars=max_chars)


async def failure_of(rewriter: QuestionRewriter, question: str = QUESTION) -> RewriteError:
    with pytest.raises(RewriteError) as raised:
        await rewriter.rewrite(question, HISTORY)
    return raised.value


def assert_clean(error: RewriteError, *forbidden: str) -> None:
    """A fixed message, nothing of the request or of the answer, no other exception attached."""
    text = f"{error!s} {error!r}"
    for secret in (QUESTION, KEY, REWRITTEN, "failure on purpose", *HISTORY_TEXTS, *forbidden):
        assert secret not in text
    assert error.__cause__ is None
    assert error.__context__ is None


HISTORY_TEXTS = [item.content for item in HISTORY]


# AC-009: the request ------------------------------------------------------------------------------------------------


async def test_AC_009_the_request_has_model_messages_and_stream_false_with_the_key() -> None:
    with fake_rewrite_provider(answer=REWRITTEN) as provider:
        assert await make_rewriter(provider, api_key=KEY).rewrite(QUESTION, HISTORY) == REWRITTEN

    (request,) = provider.requests
    assert (request.method, request.path) == ("POST", "/v1/chat/completions")
    assert request.authorization == f"Bearer {KEY}"
    assert request.body == {
        "model": MODEL,
        "messages": build_rewrite_messages(QUESTION, HISTORY),
        "stream": False,
    }


@pytest.mark.parametrize("key", [None, "", "   "])
async def test_AC_009_without_a_key_there_is_no_authorization_header(key: str | None) -> None:
    with fake_rewrite_provider(answer=REWRITTEN) as provider:
        await make_rewriter(provider, api_key=key).rewrite(QUESTION, HISTORY)

    assert provider.requests[0].authorization is None


async def test_a_trailing_slash_in_the_base_url_is_ignored() -> None:
    with fake_rewrite_provider(answer=REWRITTEN) as provider:
        await make_rewriter(provider.url + "/").rewrite(QUESTION, HISTORY)

    assert provider.requests[0].path == "/v1/chat/completions"


def test_AC_009_the_messages_are_the_instructions_the_history_in_order_and_the_question_last() -> None:
    history = [
        turn("user", "Prima domanda"),
        turn("assistant", "Prima risposta"),
        turn("user", "Seconda domanda"),
        turn("assistant", "Seconda risposta"),
    ]

    assert build_rewrite_messages(QUESTION, history) == [
        {"role": "system", "content": REWRITE_INSTRUCTIONS},
        {"role": "user", "content": "Prima domanda"},
        {"role": "assistant", "content": "Prima risposta"},
        {"role": "user", "content": "Seconda domanda"},
        {"role": "assistant", "content": "Seconda risposta"},
        {"role": "user", "content": QUESTION},
    ]


def test_AC_009_a_history_message_without_text_is_left_out_as_in_phase_3() -> None:
    history = [turn("user", "Domanda"), turn("assistant", "  \n "), turn("assistant", "Risposta")]

    assert [m["content"] for m in build_rewrite_messages(QUESTION, history)[1:]] == ["Domanda", "Risposta", QUESTION]


def test_AC_009_the_instructions_ask_for_the_standalone_question_only_in_its_language_unchanged() -> None:
    instructions = " ".join(REWRITE_INSTRUCTIONS.lower().split())

    assert "standalone question" in instructions
    assert "only the" in instructions  # return only the question
    assert "same language" in instructions
    assert "do not answer" in instructions
    assert "do not add" in instructions  # no information that is not in the conversation
    assert "do not change" in instructions and "meaning" in instructions
    # the question is data of the last message, not part of the instructions
    assert QUESTION not in REWRITE_INSTRUCTIONS


# The text received ---------------------------------------------------------------------------------------------------


async def test_the_text_is_given_without_the_spaces_at_both_ends() -> None:
    with fake_rewrite_provider(answer=f"  \n {REWRITTEN} \t\n") as provider:
        assert await make_rewriter(provider).rewrite(QUESTION, HISTORY) == REWRITTEN


@pytest.mark.parametrize(
    "text",
    [
        'Ecco la domanda: "Quanto costa la spedizione del prodotto X?"',
        "Quanto costa\nla spedizione\ndel prodotto X?",
        "«Quanto costa la spedizione?» – domanda autonoma",
    ],
)
async def test_a_text_over_several_lines_with_quotes_or_a_preamble_is_given_as_it_is(text: str) -> None:
    """Review focus: nothing is cut or cleaned beyond the spaces at both ends: «Ricerca: …» shows what was searched."""
    with fake_rewrite_provider(answer=text) as provider:
        assert await make_rewriter(provider).rewrite(QUESTION, HISTORY) == text


async def test_a_text_of_exactly_max_chars_is_accepted() -> None:
    text = "x" * MAX_CHARS
    with fake_rewrite_provider(answer=f"  {text}  ") as provider:
        assert await make_rewriter(provider).rewrite(QUESTION, HISTORY) == text


# AC-011: the failures ------------------------------------------------------------------------------------------------


async def test_AC_011_a_service_that_cannot_be_reached_is_unreachable() -> None:
    error = await failure_of(make_rewriter(closed_port_url(), api_key=KEY))

    assert error.reason == "unreachable"
    assert_clean(error)


async def test_AC_011_a_connection_closed_without_an_answer_is_unreachable() -> None:
    with fake_rewrite_provider(drop_connection=True) as provider:
        error = await failure_of(make_rewriter(provider, api_key=KEY))

    assert error.reason == "unreachable"
    assert_clean(error)


@pytest.mark.parametrize("status", [400, 401, 404, 429, 500, 503])
async def test_AC_011_a_status_that_is_not_2xx_is_http_error(status: int) -> None:
    with fake_rewrite_provider(fail_status=status) as provider:
        error = await failure_of(make_rewriter(provider, api_key=KEY))

    assert error.reason == "http_error"
    assert_clean(error)


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
async def test_a_redirect_is_http_error_and_nothing_is_sent_to_its_target(status: int) -> None:
    with fake_rewrite_provider(answer=REWRITTEN) as target:
        with fake_rewrite_provider(redirect_to=f"{target.url}/chat/completions", redirect_status=status) as provider:
            error = await failure_of(make_rewriter(provider, api_key=KEY))

    assert error.reason == "http_error"
    assert len(provider.requests) == 1
    assert target.requests == []  # neither the question nor the key went to the target of the redirect


async def test_AC_011_a_service_slower_than_the_timeout_is_timeout_after_the_timeout() -> None:
    with fake_rewrite_provider(answer=REWRITTEN, delay_seconds=5) as provider:
        started = time.monotonic()
        error = await failure_of(make_rewriter(provider, timeout=1))
        elapsed = time.monotonic() - started

    assert error.reason == "timeout"
    assert 0.9 <= elapsed < 3
    assert_clean(error)


async def test_the_timeout_covers_the_body_too_not_only_the_headers() -> None:
    """One deadline for the whole request: headers that arrive at once and a body that does not are a timeout."""
    with fake_rewrite_provider(answer=REWRITTEN, body_delay_seconds=5) as provider:
        started = time.monotonic()
        error = await failure_of(make_rewriter(provider, timeout=1))
        elapsed = time.monotonic() - started

    assert error.reason == "timeout"
    assert elapsed < 3


async def test_an_answer_within_the_timeout_is_not_cut() -> None:
    with fake_rewrite_provider(answer=REWRITTEN, delay_seconds=0.3) as provider:
        assert await make_rewriter(provider, timeout=2).rewrite(QUESTION, HISTORY) == REWRITTEN


@pytest.mark.parametrize("text", ["", "   ", "\n\t "])
async def test_AC_011_an_empty_text_is_invalid_response(text: str) -> None:
    with fake_rewrite_provider(answer=text) as provider:
        error = await failure_of(make_rewriter(provider))

    assert error.reason == "invalid_response"


async def test_AC_011_a_text_longer_than_max_chars_is_invalid_response() -> None:
    with fake_rewrite_provider(answer="x" * (MAX_CHARS + 1)) as provider:
        error = await failure_of(make_rewriter(provider))

    assert error.reason == "invalid_response"
    assert_clean(error, "x" * 50)


async def test_the_limit_is_the_max_chars_given() -> None:
    with fake_rewrite_provider(answer="abcdef") as provider:
        error = await failure_of(make_rewriter(provider, max_chars=5))

    assert error.reason == "invalid_response"


@pytest.mark.parametrize(
    "text", ["Quanto\x00 costa la spedizione?", "Quanto \ud800 costa la spedizione?", "\udfff"], ids=repr
)
async def test_a_text_that_the_database_cannot_hold_is_invalid_response(text: str) -> None:
    """Review focus: a NUL or a lone surrogate (sent as JSON escapes) fails the checks of a question of phase 3."""
    with fake_rewrite_provider(answer=text) as provider:
        error = await failure_of(make_rewriter(provider))

    assert error.reason == "invalid_response"
    assert error.__context__ is None


@pytest.mark.parametrize(
    "body",
    [
        b"not json at all",
        b'{"choices": [',
        b"\xff\xfe\x00",
        b"[]",
        b'"a string"',
        b"{}",
        b'{"choices": []}',
        b'{"choices": "x"}',
        b'{"choices": ["x"]}',
        b'{"choices": [{}]}',
        b'{"choices": [{"message": null}]}',
        b'{"choices": [{"message": "x"}]}',
        b'{"choices": [{"message": {}}]}',
        b'{"choices": [{"message": {"content": null}}]}',
        b'{"choices": [{"message": {"content": 42}}]}',
        b'{"choices": [{"message": {"content": ["a"]}}]}',
        b'{"error": {"message": "failure on purpose"}}',
    ],
)
async def test_a_body_that_is_not_a_completion_is_invalid_response(body: bytes) -> None:
    with fake_rewrite_provider(raw_body=body) as provider:
        error = await failure_of(make_rewriter(provider, api_key=KEY))

    assert error.reason == "invalid_response"
    assert_clean(error)


async def test_a_huge_body_is_invalid_response() -> None:
    body: Any = completion_body(REWRITTEN)
    body["padding"] = "p" * (2 * 1024 * 1024)
    with fake_rewrite_provider(json_body=body) as provider:
        error = await failure_of(make_rewriter(provider))

    assert error.reason == "invalid_response"


async def test_a_key_with_a_non_ascii_character_is_unreachable_without_leaking_it() -> None:
    key = "sk-chiavè-segreta"
    with fake_rewrite_provider(answer=REWRITTEN) as provider:
        error = await failure_of(make_rewriter(provider, api_key=key))

    assert error.reason == "unreachable"
    assert_clean(error, key)


async def test_a_base_url_that_is_not_a_url_is_unreachable() -> None:
    error = await failure_of(make_rewriter("not a url"))

    assert error.reason == "unreachable"


@pytest.mark.parametrize("reason", ["unreachable", "http_error", "timeout", "invalid_response"])
def test_rewrite_error_carries_its_reason_and_a_fixed_message(reason: Any) -> None:
    error = RewriteError(reason)

    assert error.reason == reason
    assert str(error)


def test_the_key_is_not_shown_by_the_rewriter_or_its_attributes() -> None:
    rewriter = make_rewriter("http://127.0.0.1:9/v1", api_key=KEY)

    assert KEY not in repr(rewriter)
    assert KEY not in repr(vars(rewriter))
