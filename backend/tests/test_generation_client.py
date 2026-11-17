# SPEC-2026-11-17-fase-3-chat
"""The streaming client of an OpenAI-compatible provider (T-01): AC-003, and AC-004 and AC-005 through GET /api/health.

The client runs against `fake_generation_provider` (real sockets). The tests of the plan's "Review focus" on abnormal
provider streams have no AC of T-01: a stream is either read whole or it is `generation_error`/`generation_timeout`.
Decision for the stream that ends without `[DONE]`: it finished normally only if a `finish_reason` was seen; otherwise
it is `generation_error`, even when text was received (the text already yielded is the caller's to discard).
"""

from __future__ import annotations

import asyncio
import threading
import time
import traceback
from collections.abc import Callable, Iterator
from contextlib import ExitStack
from typing import Any

import httpx
import pytest
from redis import Redis as SyncRedis

from app.chat.generation import ChatMessage, GenerationError, OpenAICompatibleClient
from app.config import Settings
from tests.conftest import TEST_REDIS_URL, make_settings
from tests.fakes.http_server import closed_port_url, fake_service
from tests.fakes.services import (
    FakeGenerationProvider,
    GenerationStep,
    delta_event,
    fake_generation_provider,
    finish_event,
    sse_data,
)

QUESTION = "Qual è il prezzo del servizio? ☃"
MESSAGES: list[ChatMessage] = [
    {"role": "system", "content": "Rispondi solo in base alle fonti."},
    {"role": "user", "content": QUESTION},
]
MODEL = "modello-di-prova"
KEY = "sk-chiave-segreta-123"
DONE = sse_data("[DONE]")
FIXED_MESSAGES = {
    "generation_error": "The generation provider failed or sent an invalid answer",
    "generation_timeout": "The generation provider did not finish in time",
}


def make_client(
    provider: FakeGenerationProvider | str, api_key: str | None = None, timeout: float = 10.0
) -> OpenAICompatibleClient:
    url = provider if isinstance(provider, str) else provider.url
    return OpenAICompatibleClient(base_url=url, model=MODEL, api_key=api_key, timeout=timeout)


async def read_all(client: OpenAICompatibleClient) -> tuple[list[str], GenerationError | None]:
    """The fragments received and the error that ended the stream, if any."""
    fragments: list[str] = []
    try:
        async for fragment in client.stream(MESSAGES):
            fragments.append(fragment)
    except GenerationError as error:
        return fragments, error
    return fragments, None


async def fragments_of(provider: FakeGenerationProvider) -> list[str]:
    fragments, error = await read_all(make_client(provider))
    assert error is None
    return fragments


def assert_fixed_message(error: GenerationError, *forbidden: str) -> None:
    """The message is fixed text: never the prompt, the response body or the key."""
    text = f"{error!s} {error!r}"
    for secret in (QUESTION, KEY, "failure on purpose", *forbidden):
        assert secret not in text


# The request (AC-003) -----------------------------------------------------------------------------------------------


async def test_SPEC_2026_11_17_fase_3_chat_AC_003_the_request_has_model_messages_stream_and_key() -> None:
    with fake_generation_provider() as provider:
        fragments, error = await read_all(make_client(provider, api_key=KEY))

    assert error is None
    assert fragments == ["Ciao", " mondo"]
    [request] = provider.requests
    assert (request.method, request.path) == ("POST", "/v1/chat/completions")
    assert request.authorization == f"Bearer {KEY}"
    assert request.body == {"model": MODEL, "messages": MESSAGES, "stream": True}


@pytest.mark.parametrize("api_key", [None, ""])
async def test_SPEC_2026_11_17_fase_3_chat_AC_003_without_a_key_there_is_no_authorization_header(
    api_key: str | None,
) -> None:
    with fake_generation_provider() as provider:
        fragments, error = await read_all(make_client(provider, api_key=api_key))

    assert error is None
    assert fragments == ["Ciao", " mondo"]
    [request] = provider.requests
    assert request.authorization is None
    assert request.body["model"] == MODEL


async def test_a_trailing_slash_in_the_base_url_is_ignored() -> None:
    with fake_generation_provider() as provider:
        client = make_client(provider.url + "/")
        assert await read_all(client) == (["Ciao", " mondo"], None)

    assert [request.path for request in provider.requests] == ["/v1/chat/completions"]


# Streaming ----------------------------------------------------------------------------------------------------------


async def test_the_fragments_arrive_in_order_one_per_event() -> None:
    with fake_generation_provider(fragments=["uno", " due", " tre", " quattro"]) as provider:
        assert await fragments_of(provider) == ["uno", " due", " tre", " quattro"]


async def test_the_first_fragment_is_yielded_before_the_provider_has_sent_the_second() -> None:
    gate = threading.Event()
    script: list[GenerationStep] = [delta_event("uno"), gate, delta_event(" due"), finish_event(), DONE]
    with fake_generation_provider(script=script) as provider:
        stream = make_client(provider).stream(MESSAGES)
        try:
            first = await asyncio.wait_for(anext(stream), 5)
            assert first == "uno"
            assert not gate.is_set()  # the provider is still holding back the rest
            gate.set()
            assert [fragment async for fragment in stream] == [" due"]
        finally:
            gate.set()
            await stream.aclose()


async def test_closing_the_stream_early_closes_the_connection_to_the_provider() -> None:
    script: list[GenerationStep] = [delta_event("uno")] + [0.1, delta_event("x")] * 100
    with fake_generation_provider(script=script) as provider:
        stream = make_client(provider).stream(MESSAGES)
        assert await asyncio.wait_for(anext(stream), 5) == "uno"
        await stream.aclose()

        assert await asyncio.to_thread(provider.client_closed.wait, 5)


# Abnormal streams (plan Review focus) -------------------------------------------------------------------------------


async def test_a_data_line_split_between_packets_is_read_whole() -> None:
    event = delta_event("Ciao mondo")
    cuts = [10, 40, len(event) - 3]  # inside "data: ", inside the JSON, inside the final blank lines
    script: list[GenerationStep] = []
    start = 0
    for cut in cuts:
        script += [event[start:cut], 0.05]
        start = cut
    script += [event[start:], finish_event(), DONE]

    with fake_generation_provider(script=script) as provider:
        assert await fragments_of(provider) == ["Ciao mondo"]


async def test_a_multibyte_character_split_between_packets_is_not_corrupted() -> None:
    text = "è ☃ 😀 fine"
    event = delta_event(text)
    # one cut in the middle of each of the 2-, 3- and 4-byte characters
    cuts = [event.index("è".encode()) + 1, event.index("☃".encode()) + 1, event.index("😀".encode()) + 2]
    script: list[GenerationStep] = []
    start = 0
    for cut in cuts:
        piece = event[start:cut]
        with pytest.raises(UnicodeDecodeError):
            piece.decode("utf-8")  # the fixture really splits a character
        script += [piece, 0.05]
        start = cut
    script += [event[start:], finish_event(), DONE]

    with fake_generation_provider(script=script) as provider:
        assert await fragments_of(provider) == [text]


async def test_unicode_line_separators_inside_the_text_do_not_end_the_line() -> None:
    # U+2028, U+2029 and U+0085 are line ends for str.splitlines (and so for aiter_lines), but not for an event stream
    text = "a b c\x85d"
    with fake_generation_provider(script=[delta_event(text), finish_event(), DONE]) as provider:
        assert await fragments_of(provider) == [text]


async def test_crlf_line_endings_are_accepted() -> None:
    body = (delta_event("Ciao") + finish_event() + DONE).replace(b"\n", b"\r\n")
    with fake_generation_provider(script=[body]) as provider:
        assert await fragments_of(provider) == ["Ciao"]


async def test_events_without_text_are_skipped() -> None:
    script: list[GenerationStep] = [
        b": keepalive\n\n",
        b"event: message\nid: 7\nretry: 100\n",
        sse_data({"id": "x", "choices": []}),  # empty choices (Azure style prompt filter, or usage only)
        sse_data({"choices": [{"index": 0, "delta": {"role": "assistant"}}]}),  # role only
        sse_data({"choices": [{"index": 0, "delta": {"content": None}}]}),
        sse_data({"choices": [{"index": 0, "delta": {"content": ""}}]}),
        sse_data({"choices": [{"index": 0}]}),  # no delta at all
        sse_data({"usage": {"total_tokens": 3}}),  # no choices key: metadata
        delta_event("Ciao"),
        sse_data({"choices": [{"index": 0, "delta": {}, "finish_reason": None}]}),
        finish_event(),
        DONE,
    ]
    with fake_generation_provider(script=script) as provider:
        assert await fragments_of(provider) == ["Ciao"]


async def test_only_the_first_choice_is_read() -> None:
    choices = [{"index": 0, "delta": {"content": "primo"}}, {"index": 1, "delta": {"content": "secondo"}}]
    with fake_generation_provider(script=[sse_data({"choices": choices}), finish_event(), DONE]) as provider:
        assert await fragments_of(provider) == ["primo"]


async def test_an_event_that_carries_text_and_finish_reason_together_keeps_the_text() -> None:
    with fake_generation_provider(
        script=[delta_event("Ciao"), delta_event("!", finish_reason="stop"), DONE]
    ) as provider:
        assert await fragments_of(provider) == ["Ciao", "!"]


async def test_nothing_after_done_is_read() -> None:
    with fake_generation_provider(
        script=[delta_event("Ciao"), finish_event(), DONE, b"data: {not json\n\n"]
    ) as provider:
        assert await read_all(make_client(provider)) == (["Ciao"], None)


async def test_a_stream_with_finish_reason_but_without_done_ends_normally() -> None:
    with fake_generation_provider(script=[delta_event("Ciao"), finish_event()]) as provider:
        assert await read_all(make_client(provider)) == (["Ciao"], None)


async def test_a_last_done_without_final_newline_ends_normally() -> None:
    with fake_generation_provider(script=[delta_event("Ciao"), b"data: [DONE]"]) as provider:
        assert await read_all(make_client(provider)) == (["Ciao"], None)


async def test_a_stream_closed_without_done_and_without_finish_reason_is_generation_error() -> None:
    with fake_generation_provider(script=[delta_event("Ciao"), delta_event(" mondo")]) as provider:
        fragments, error = await read_all(make_client(provider, api_key=KEY))

    assert fragments == ["Ciao", " mondo"]  # the caller discards the partial text
    assert error is not None
    assert error.code == "generation_error"
    assert_fixed_message(error)


async def test_an_empty_stream_is_generation_error() -> None:
    with fake_generation_provider(script=[]) as provider:
        fragments, error = await read_all(make_client(provider))

    assert (fragments, error is not None and error.code) == ([], "generation_error")


async def test_a_stream_broken_in_the_middle_of_an_event_is_generation_error() -> None:
    event = delta_event("Ciao mondo")
    with fake_generation_provider(script=[delta_event("Ciao"), event[:25]], abort_after_body=True) as provider:
        fragments, error = await read_all(make_client(provider))

    assert fragments == ["Ciao"]
    assert error is not None
    assert error.code == "generation_error"


async def test_a_connection_dropped_after_a_whole_event_is_generation_error() -> None:
    with fake_generation_provider(script=[delta_event("Ciao")], abort_after_body=True) as provider:
        fragments, error = await read_all(make_client(provider))

    assert fragments == ["Ciao"]
    assert error is not None
    assert error.code == "generation_error"


async def test_an_error_object_inside_the_stream_is_generation_error_without_its_text() -> None:
    error_event = sse_data({"error": {"message": "il modello ha detto SEGRETO", "type": "server_error"}})
    with fake_generation_provider(script=[delta_event("Ciao"), error_event, delta_event("dopo"), DONE]) as provider:
        fragments, error = await read_all(make_client(provider))

    assert fragments == ["Ciao"]  # nothing after the error is read
    assert error is not None
    assert error.code == "generation_error"
    assert_fixed_message(error, "SEGRETO")


@pytest.mark.parametrize(
    "bad",
    [
        pytest.param(b"data: {non e' json\n\n", id="malformed-json"),
        pytest.param(b"data: \n\n", id="empty-data"),
        pytest.param(b"data: [1, 2]\n\n", id="json-not-an-object"),
        pytest.param(sse_data({"choices": "no"}), id="choices-not-a-list"),
        pytest.param(sse_data({"choices": ["no"]}), id="choice-not-an-object"),
        pytest.param(sse_data({"choices": [{"delta": "no"}]}), id="delta-not-an-object"),
        pytest.param(sse_data({"choices": [{"delta": {"content": ["x"]}}]}), id="content-not-a-string"),
        pytest.param(sse_data({"choices": [{"delta": {}, "finish_reason": "error"}]}), id="finish-reason-error"),
        pytest.param(b'{"error": {"message": "x"}}\n', id="json-body-without-data-prefix"),
        pytest.param(b"\xff\xfe\n\n", id="invalid-utf8"),
        pytest.param(b"data: " + b"x" * (1024 * 1024 + 10) + b"\n\n", id="line-too-long"),
    ],
)
async def test_a_malformed_event_is_generation_error(bad: bytes) -> None:
    with fake_generation_provider(
        script=[delta_event("Ciao"), bad, delta_event("dopo"), finish_event(), DONE]
    ) as provider:
        fragments, error = await read_all(make_client(provider, api_key=KEY))

    assert fragments == ["Ciao"]  # what was valid before is delivered; nothing after the bad event is
    assert error is not None
    assert error.code == "generation_error"
    assert_fixed_message(error)


# Errors before the stream -------------------------------------------------------------------------------------------


@pytest.mark.parametrize("status", [400, 401, 404, 429, 500, 503])
async def test_a_status_that_is_not_2xx_is_generation_error_without_the_body(status: int) -> None:
    with fake_generation_provider(fail_status=status) as provider:
        fragments, error = await read_all(make_client(provider, api_key=KEY))

    assert fragments == []
    assert error is not None
    assert error.code == "generation_error"
    assert_fixed_message(error)


@pytest.mark.parametrize(
    "body",
    [
        {"error": {"message": "failure on purpose", "type": "invalid_request_error"}},
        {"id": "x", "object": "chat.completion", "choices": [{"message": {"role": "assistant", "content": "Ciao"}}]},
        "failure on purpose",
    ],
    ids=["error-body", "answer-without-stream", "json-string"],
)
async def test_a_200_with_a_json_body_instead_of_a_stream_is_generation_error(body: Any) -> None:
    with fake_generation_provider(json_body=body) as provider:
        fragments, error = await read_all(make_client(provider, api_key=KEY))

    assert fragments == []
    assert error is not None
    assert error.code == "generation_error"
    assert_fixed_message(error)


async def test_a_provider_that_cannot_be_reached_is_generation_error() -> None:
    fragments, error = await read_all(make_client(closed_port_url() + "/v1", api_key=KEY))

    assert fragments == []
    assert error is not None
    assert error.code == "generation_error"
    assert_fixed_message(error)


async def test_a_connection_closed_without_an_answer_is_generation_error() -> None:
    with fake_generation_provider(drop_connection=True) as provider:
        fragments, error = await read_all(make_client(provider))

    assert fragments == []
    assert error is not None
    assert error.code == "generation_error"


async def test_a_base_url_that_is_not_a_url_is_generation_error() -> None:
    fragments, error = await read_all(make_client("non-un-url"))

    assert fragments == []
    assert error is not None
    assert error.code == "generation_error"


# Timeout ------------------------------------------------------------------------------------------------------------


async def test_a_provider_that_does_not_answer_is_generation_timeout_after_the_timeout() -> None:
    with fake_generation_provider(delay_seconds=30) as provider:
        started = time.monotonic()
        fragments, error = await read_all(make_client(provider, timeout=0.5))
        elapsed = time.monotonic() - started

    assert fragments == []
    assert error is not None
    assert error.code == "generation_timeout"
    assert 0.4 <= elapsed < 3.0
    assert_fixed_message(error)


async def test_a_stream_that_stalls_after_the_first_fragment_is_generation_timeout() -> None:
    stall = threading.Event()
    try:
        with fake_generation_provider(script=[delta_event("Ciao"), stall]) as provider:
            fragments, error = await read_all(make_client(provider, timeout=0.5))
    finally:
        stall.set()

    assert fragments == ["Ciao"]
    assert error is not None
    assert error.code == "generation_timeout"


async def test_the_timeout_bounds_the_whole_generation_not_the_silence_between_fragments() -> None:
    # a fragment every 0.2 s for 4 s: no pause is longer than the timeout, the generation as a whole is
    script: list[GenerationStep] = []
    for number in range(20):
        script += [0.2, delta_event(f"{number} ")]
    script += [finish_event(), DONE]
    with fake_generation_provider(script=script) as provider:
        started = time.monotonic()
        fragments, error = await read_all(make_client(provider, timeout=1.0))
        elapsed = time.monotonic() - started

    assert error is not None
    assert error.code == "generation_timeout"
    assert 0 < len(fragments) < 20
    assert elapsed < 3.0


async def test_a_generation_that_ends_before_the_timeout_is_not_cut() -> None:
    with fake_generation_provider(fragments=["a", "b", "c"], fragment_delay_seconds=0.1) as provider:
        fragments, error = await read_all(make_client(provider, timeout=5.0))

    assert (fragments, error) == (["a", "b", "c"], None)


async def test_the_task_can_be_cancelled_while_waiting_for_the_provider() -> None:
    with fake_generation_provider(delay_seconds=30) as provider:
        task = asyncio.ensure_future(read_all(make_client(provider, timeout=60)))
        assert await asyncio.to_thread(provider.request_received.wait, 5)  # the client now waits for the answer
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


# GenerationError ----------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("code", ["generation_error", "generation_timeout"])
def test_generation_error_carries_its_code_and_a_fixed_message(code: Any) -> None:
    error = GenerationError(code)

    assert error.code == code
    assert str(error) == FIXED_MESSAGES[code]
    assert isinstance(error, Exception)


# check() for the health of the provider (AC-005) ----------------------------------------------------------------------


async def test_SPEC_2026_11_17_fase_3_chat_AC_005_check_is_ok_when_models_answers_2xx() -> None:
    with fake_generation_provider() as provider:
        state = await make_client(provider).check(2.0)

    assert state == "ok"
    [request] = provider.requests
    assert (request.method, request.path) == ("GET", "/v1/models")
    assert request.authorization is None


async def test_SPEC_2026_11_17_fase_3_chat_AC_005_check_sends_the_bearer_key_when_there_is_one() -> None:
    with fake_generation_provider() as provider:
        assert await make_client(provider, api_key=KEY).check(2.0) == "ok"

    assert provider.requests[0].authorization == f"Bearer {KEY}"


@pytest.mark.parametrize("status", [301, 401, 404, 500, 503])
async def test_SPEC_2026_11_17_fase_3_chat_AC_005_check_is_unreachable_for_a_status_that_is_not_2xx(
    status: int,
) -> None:
    with fake_generation_provider(models_status=status) as provider:
        assert await make_client(provider).check(2.0) == "unreachable"


async def test_SPEC_2026_11_17_fase_3_chat_AC_005_check_is_unreachable_when_nothing_listens() -> None:
    assert await make_client(closed_port_url() + "/v1").check(2.0) == "unreachable"


async def test_SPEC_2026_11_17_fase_3_chat_AC_005_check_is_unreachable_in_time_for_a_slow_provider() -> None:
    with fake_generation_provider(models_delay_seconds=30) as provider:
        started = time.monotonic()
        state = await make_client(provider, timeout=300).check(0.5)  # the timeout of `check` is the argument
        elapsed = time.monotonic() - started

    assert state == "unreachable"
    assert elapsed < 2.0


async def test_check_never_raises_for_a_base_url_that_is_not_a_url() -> None:
    assert await make_client("non-un-url").check(1.0) == "unreachable"


# Phase 3: the `generation` check ------------------------------------------------------------------------------------


@pytest.fixture
def worker_heartbeat() -> Iterator[None]:
    """A worker heartbeat key in Redis, so that `worker` is `ok` and `generation` is the only check that is not."""
    connection = SyncRedis.from_url(TEST_REDIS_URL)
    key = "ingestion:worker:alive:test-generation-health"
    connection.set(key, "1", ex=30)
    yield
    connection.delete(key)
    connection.close()


class RecordedRequests:
    """Every HTTP request any httpx client of the process sends while a test runs (the URLs, in order)."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.urls: list[str] = []
        send = httpx.AsyncClient.send
        urls = self.urls

        async def recording_send(self: httpx.AsyncClient, request: httpx.Request, **kwargs: object) -> httpx.Response:
            if request.url.host != "testserver":  # the test's own calls to the application
                urls.append(str(request.url))
            return await send(self, request, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(httpx.AsyncClient, "send", recording_send)


async def test_SPEC_2026_11_17_fase_3_chat_AC_004_no_provider_is_not_configured_and_degraded(
    client_for: Callable[[Settings], httpx.AsyncClient], worker_heartbeat: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    sent = RecordedRequests(monkeypatch)
    with fake_service() as ollama, fake_service() as docling:
        settings = make_settings(ollama_base_url=ollama, docling_base_url=docling)
        assert settings.generation_base_url == ""
        async with client_for(settings) as client:
            response = await client.get("/api/health")

    assert response.status_code == 200
    assert response.json() == {
        "status": "degraded",
        "checks": {
            "database": "ok",
            "redis": "ok",
            "ollama": "ok",
            "docling": "ok",
            "worker": "ok",
            "generation": "not_configured",
        },
    }
    # only Ollama and Docling were called: nothing went out for the provider
    assert sorted(sent.urls) == sorted([f"{ollama}/api/version", f"{docling}/health"])


async def test_SPEC_2026_11_17_fase_3_chat_AC_004_model_and_key_without_a_base_url_are_still_not_configured(
    client_for: Callable[[Settings], httpx.AsyncClient], monkeypatch: pytest.MonkeyPatch
) -> None:
    sent = RecordedRequests(monkeypatch)
    settings = make_settings(generation_model="fake-model", generation_api_key="sk-unused")
    async with client_for(settings) as client:
        response = await client.get("/api/health")

    assert response.status_code == 200
    assert response.json()["checks"]["generation"] == "not_configured"
    assert not [url for url in sent.urls if url.endswith("/models")]


async def test_SPEC_2026_11_17_fase_3_chat_AC_005_generation_is_ok_then_unreachable_when_the_provider_stops(
    client_for: Callable[[Settings], httpx.AsyncClient],
) -> None:
    timeout = 1.0
    with ExitStack() as stack:
        ollama = stack.enter_context(fake_service())
        docling = stack.enter_context(fake_service())
        provider = stack.enter_context(fake_generation_provider())
        settings = make_settings(
            ollama_base_url=ollama,
            docling_base_url=docling,
            generation_base_url=provider.url,
            generation_model="fake-model",
            health_check_timeout_seconds=timeout,
        )
        async with client_for(settings) as client:
            started = time.monotonic()
            first = await client.get("/api/health")
            first_elapsed = time.monotonic() - started
            assert [(request.method, request.path) for request in provider.requests] == [("GET", "/v1/models")]

            stack.pop_all().close()  # the fake services and the provider stop: the same application is asked again
            started = time.monotonic()
            second = await client.get("/api/health")
            second_elapsed = time.monotonic() - started

    assert first.status_code == 200
    assert first.json()["checks"]["generation"] == "ok"
    assert second.status_code == 200
    assert second.json()["checks"]["generation"] == "unreachable"
    assert second.json()["status"] == "degraded"
    assert first_elapsed <= timeout + 1.0
    assert second_elapsed <= timeout + 1.0


async def test_SPEC_2026_11_17_fase_3_chat_AC_005_a_slow_provider_is_unreachable_within_the_health_timeout(
    client_for: Callable[[Settings], httpx.AsyncClient],
) -> None:
    timeout = 1.0
    with (
        fake_service() as ollama,
        fake_service() as docling,
        fake_generation_provider(models_delay_seconds=30) as provider,
    ):
        settings = make_settings(
            ollama_base_url=ollama,
            docling_base_url=docling,
            generation_base_url=provider.url,
            generation_model="fake-model",
            # a long generation timeout must not stretch the health check: it uses HEALTH_CHECK_TIMEOUT_SECONDS
            generation_timeout_seconds=300,
            health_check_timeout_seconds=timeout,
        )
        async with client_for(settings) as client:
            started = time.monotonic()
            response = await client.get("/api/health")
            elapsed = time.monotonic() - started

    assert response.status_code == 200
    assert response.json()["checks"]["generation"] == "unreachable"
    assert elapsed <= timeout + 1.0


@pytest.mark.parametrize("status", [401, 500])
async def test_SPEC_2026_11_17_fase_3_chat_AC_005_a_provider_that_errors_on_models_is_unreachable(
    client_for: Callable[[Settings], httpx.AsyncClient], status: int
) -> None:
    with fake_generation_provider(models_status=status) as provider:
        settings = make_settings(generation_base_url=provider.url, generation_model="fake-model")
        async with client_for(settings) as client:
            response = await client.get("/api/health")

    assert response.status_code == 200
    assert response.json()["checks"]["generation"] == "unreachable"


async def test_SPEC_2026_11_17_fase_3_chat_AC_005_the_health_check_sends_the_key_to_the_provider_only(
    client_for: Callable[[Settings], httpx.AsyncClient],
) -> None:
    with fake_service() as ollama, fake_generation_provider() as provider:
        settings = make_settings(
            ollama_base_url=ollama,
            generation_base_url=provider.url,
            generation_model="fake-model",
            generation_api_key="sk-health-key",
        )
        async with client_for(settings) as client:
            response = await client.get("/api/health")

    assert response.json()["checks"]["generation"] == "ok"
    assert [request.authorization for request in provider.requests] == ["Bearer sk-health-key"]


async def test_SPEC_2026_11_17_fase_3_chat_AC_004_the_http_code_is_still_decided_by_database_and_redis_only(
    client_for: Callable[[Settings], httpx.AsyncClient],
) -> None:
    settings = make_settings(
        redis_url=closed_port_url("redis") + "/0", generation_base_url=closed_port_url() + "/v1", generation_model="m"
    )
    async with client_for(settings) as client:
        response = await client.get("/api/health")

    assert response.status_code == 503
    assert response.json()["checks"]["redis"] == "unreachable"
    assert response.json()["checks"]["generation"] == "unreachable"


async def test_SPEC_2026_11_17_fase_3_chat_AC_003_an_empty_key_sends_no_authorization_to_the_provider(
    client_for: Callable[[Settings], httpx.AsyncClient],
) -> None:
    with fake_generation_provider() as provider:
        settings = make_settings(generation_base_url=provider.url, generation_model="fake-model", generation_api_key="")
        async with client_for(settings) as client:
            response = await client.get("/api/health")

    assert response.json()["checks"]["generation"] == "ok"
    assert [request.authorization for request in provider.requests] == [None]


# Review of T-01 (sdd-reviewer, 2026-11-17): decisions confirmed by the owner and fixes of the review ---------------
# The spec is silent on the first three groups; the owner confirmed them on 2026-11-17 (see the PR of T-01).


async def read_all_any(
    client: OpenAICompatibleClient, messages: list[ChatMessage] | None = None
) -> tuple[list[str], GenerationError]:
    """Like `read_all`, but any other exception is a failure of the test (named by type: its text can hold secrets)."""
    fragments: list[str] = []
    try:
        async for fragment in client.stream(MESSAGES if messages is None else messages):
            fragments.append(fragment)
    except GenerationError as error:
        return fragments, error
    except Exception as other:
        pytest.fail(f"stream() raised {type(other).__name__} instead of GenerationError")
    pytest.fail("stream() ended without an error")


def assert_nothing_chained(error: BaseException, *secrets: str) -> None:
    """The error holds no other exception (their repr can carry the key or the prompt) and shows no secret."""
    assert error.__cause__ is None
    assert error.__context__ is None
    rendered = "".join(traceback.format_exception(error)) + repr(error) + str(error) + repr(error.args)
    for secret in secrets:
        assert secret not in rendered


@pytest.mark.parametrize("reason", ["length", "content_filter", "tool_calls", "function_call", "error", "novel"])
async def test_a_finish_reason_other_than_stop_is_generation_error_even_with_done(reason: str) -> None:
    with fake_generation_provider(script=[delta_event("Ciao"), finish_event(reason), DONE]) as provider:
        fragments, error = await read_all(make_client(provider, api_key=KEY))

    assert fragments == ["Ciao"]  # the caller discards the partial text: a cut answer is never a completed one
    assert error is not None
    assert error.code == "generation_error"
    assert_fixed_message(error)


@pytest.mark.parametrize("reason", ["length", "content_filter"])
async def test_a_finish_reason_that_cuts_the_answer_is_generation_error_without_done_too(reason: str) -> None:
    with fake_generation_provider(script=[delta_event("Ciao"), finish_event(reason)]) as provider:
        _, error = await read_all(make_client(provider))

    assert error is not None
    assert error.code == "generation_error"


async def test_a_finish_reason_in_the_event_that_carries_the_text_is_judged_the_same() -> None:
    with fake_generation_provider(script=[delta_event("Ciao"), delta_event("!", finish_reason="length"), DONE]) as p:
        _, error = await read_all(make_client(p))

    assert error is not None
    assert error.code == "generation_error"


async def test_a_finish_reason_other_than_stop_ends_the_reading_there() -> None:
    script = [delta_event("Ciao"), finish_event("length"), delta_event("dopo"), finish_event(), DONE]
    with fake_generation_provider(script=script) as provider:
        fragments, error = await read_all(make_client(provider))

    assert fragments == ["Ciao"]
    assert error is not None


@pytest.mark.parametrize(
    "script",
    [
        pytest.param([finish_event(), DONE], id="finish-and-done"),
        pytest.param([DONE], id="only-done"),
        pytest.param([finish_event()], id="only-finish"),
        pytest.param([delta_event(""), delta_event(""), finish_event(), DONE], id="empty-fragments"),
        pytest.param([sse_data({"choices": [{"delta": {"content": None}}]}), finish_event(), DONE], id="null-content"),
        pytest.param([sse_data({"choices": []}), sse_data({"usage": {"total_tokens": 1}}), DONE], id="metadata-only"),
        pytest.param([b": keepalive\n\n", b"event: message\n\n", DONE], id="comments-only"),
    ],
)
async def test_a_stream_that_ends_normally_without_any_text_is_generation_error(script: list[GenerationStep]) -> None:
    with fake_generation_provider(script=script) as provider:
        fragments, error = await read_all(make_client(provider, api_key=KEY))

    assert fragments == []
    assert error is not None
    assert error.code == "generation_error"
    assert_fixed_message(error)


async def test_a_single_fragment_of_white_space_is_text() -> None:
    with fake_generation_provider(script=[delta_event(" "), finish_event(), DONE]) as provider:
        assert await read_all(make_client(provider)) == ([" "], None)


async def test_a_stream_closed_with_neither_done_nor_finish_reason_stays_generation_error() -> None:
    with fake_generation_provider(script=[delta_event("Ciao"), delta_event("!")]) as provider:
        fragments, error = await read_all(make_client(provider))

    assert fragments == ["Ciao", "!"]
    assert error is not None
    assert error.code == "generation_error"


# The data field without the space after the colon -------------------------------------------------------------------


async def test_a_data_field_without_the_space_after_the_colon_is_read() -> None:
    def no_space(event: bytes) -> bytes:
        return event.replace(b"data: ", b"data:", 1)

    script = [no_space(delta_event("Ciao")), no_space(delta_event(" mondo")), no_space(finish_event()), no_space(DONE)]
    with fake_generation_provider(script=script) as provider:
        assert await read_all(make_client(provider)) == (["Ciao", " mondo"], None)


async def test_done_without_the_space_ends_the_reading() -> None:
    script = [delta_event("Ciao"), b"data:[DONE]\n\n", b"data: {not json\n\n"]
    with fake_generation_provider(script=script) as provider:
        fragments, error = await read_all(make_client(provider))

    # no finish_reason was seen, but [DONE] is the end: nothing after it is read
    assert (fragments, error) == (["Ciao"], None)


# Redirects are never followed ---------------------------------------------------------------------------------------


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
async def test_a_redirect_is_generation_error_and_nothing_is_sent_to_the_target(status: int) -> None:
    with fake_generation_provider() as target:
        with fake_generation_provider(redirect_to=f"{target.url}/chat/completions", redirect_status=status) as provider:
            fragments, error = await read_all(make_client(provider, api_key=KEY))

        assert fragments == []
        assert error is not None
        assert error.code == "generation_error"
        assert_fixed_message(error)
        assert len(provider.requests) == 1
        assert target.requests == []  # neither the prompt nor the key went to the target of the redirect


@pytest.mark.parametrize("status", [301, 307, 308])
async def test_check_does_not_follow_a_redirect_either(status: int) -> None:
    with fake_generation_provider() as target:
        with fake_generation_provider(redirect_to=f"{target.url}/models", redirect_status=status) as provider:
            assert await make_client(provider, api_key=KEY).check(2.0) == "unreachable"

        assert target.requests == []


# Cancellation closes the connection ---------------------------------------------------------------------------------


async def test_cancelling_the_consumer_mid_stream_closes_the_connection_to_the_provider() -> None:
    script: list[GenerationStep] = [delta_event("uno")] + [0.1, delta_event("x")] * 100
    with fake_generation_provider(script=script) as provider:
        first = asyncio.Event()

        async def consume() -> None:
            async for _ in make_client(provider).stream(MESSAGES):
                first.set()

        task = asyncio.ensure_future(consume())
        await asyncio.wait_for(first.wait(), 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert await asyncio.to_thread(provider.client_closed.wait, 5)


# Text that cannot be encoded: GenerationError, never the raw exception ----------------------------------------------

NON_ASCII_KEY = "sk-chiave-con-è-☃"
SURROGATE_QUESTION = "domanda riservata con un surrogato isolato \ud800 fine"


async def test_a_key_with_a_non_ascii_character_is_generation_error_without_leaking_it() -> None:
    with fake_generation_provider() as provider:
        fragments, error = await read_all_any(make_client(provider, api_key=NON_ASCII_KEY))

    assert fragments == []
    assert error.code == "generation_error"
    assert_nothing_chained(error, NON_ASCII_KEY, "chiave-con", QUESTION)
    assert provider.requests == []  # nothing was sent


async def test_a_message_with_a_lone_surrogate_is_generation_error_without_leaking_the_prompt() -> None:
    messages: list[ChatMessage] = [
        {"role": "system", "content": "Rispondi solo in base alle fonti."},
        {"role": "user", "content": SURROGATE_QUESTION},
    ]
    with fake_generation_provider() as provider:
        _, error = await read_all_any(make_client(provider, api_key=KEY), messages)

    assert error.code == "generation_error"
    assert_nothing_chained(error, "domanda riservata", "surrogato", KEY)
    assert provider.requests == []


async def test_check_with_a_non_ascii_key_is_unreachable_and_never_raises() -> None:
    with fake_generation_provider() as provider:
        assert await make_client(provider, api_key=NON_ASCII_KEY).check(2.0) == "unreachable"

    assert provider.requests == []


async def test_a_stream_error_of_httpx_is_generation_error(monkeypatch: pytest.MonkeyPatch) -> None:
    # httpx.StreamError (StreamClosed, StreamConsumed, ...) is a RuntimeError, not an httpx.HTTPError
    async def broken(self: httpx.Response) -> Any:
        raise httpx.StreamClosed()
        yield b""  # pragma: no cover (makes this an async generator)

    monkeypatch.setattr(httpx.Response, "aiter_bytes", broken)
    with fake_generation_provider() as provider:
        fragments, error = await read_all_any(make_client(provider, api_key=KEY))

    assert fragments == []
    assert error.code == "generation_error"
    assert_nothing_chained(error, KEY, QUESTION)


@pytest.mark.parametrize(
    "mode",
    [
        pytest.param({"script": [delta_event("Ciao"), b"data: {non e' json: SEGRETO\n\n"]}, id="malformed-json"),
        pytest.param({"script": [delta_event("Ciao"), b"\xff\xfe\n\n"]}, id="invalid-utf8"),
        pytest.param({"fail_status": 500}, id="status"),
        pytest.param({"json_body": {"error": {"message": "SEGRETO"}}}, id="json-body"),
        pytest.param({"drop_connection": True}, id="dropped"),
        pytest.param({"script": [delta_event("Ciao")], "abort_after_body": True}, id="broken-stream"),
    ],
)
async def test_every_generation_error_of_the_stream_carries_no_other_exception(mode: dict[str, Any]) -> None:
    with fake_generation_provider(**mode) as provider:
        _, error = await read_all(make_client(provider, api_key=KEY))

    assert error is not None
    assert error.code == "generation_error"
    assert_nothing_chained(error, KEY, QUESTION, "SEGRETO")


async def test_a_provider_that_cannot_be_reached_carries_no_other_exception() -> None:
    _, error = await read_all(make_client(closed_port_url() + "/v1", api_key=KEY))

    assert error is not None
    assert_nothing_chained(error, KEY, QUESTION)


async def test_the_timeout_error_carries_no_other_exception() -> None:
    with fake_generation_provider(delay_seconds=30) as provider:
        _, error = await read_all(make_client(provider, api_key=KEY, timeout=0.3))

    assert error is not None
    assert error.code == "generation_timeout"
    assert_nothing_chained(error, KEY, QUESTION)


# The key is not a readable attribute ----------------------------------------------------------------------------------


def test_the_key_is_not_shown_by_the_client_or_by_its_attributes() -> None:
    client = make_client("http://provider.example.internal/v1", api_key=KEY)

    assert KEY not in repr(client)
    assert KEY not in str(vars(client))
    assert KEY not in repr([getattr(client, name, None) for name in dir(client) if not name.startswith("__")])
    assert not any(value == KEY for value in vars(client).values())


async def test_the_key_is_still_sent_to_the_provider_after_being_hidden() -> None:
    with fake_generation_provider() as provider:
        assert await read_all(make_client(provider, api_key=f"  {KEY}  ")) == (["Ciao", " mondo"], None)
        assert await make_client(provider, api_key=f"  {KEY}  ").check(2.0) == "ok"

    assert [request.authorization for request in provider.requests] == [f"Bearer {KEY}"] * 2


# The deadline holds even when the data is already there ------------------------------------------------------------


async def test_the_deadline_is_enforced_even_when_the_rest_of_the_stream_is_already_buffered() -> None:
    # The whole answer is written at once; the consumer then stays away longer than the timeout. The fragments that
    # follow are in the buffers (no read has to wait), but the generation is over time: generation_timeout.
    events = [delta_event(f"parte{number} ") for number in range(50)]
    with fake_generation_provider(script=[*events, finish_event(), DONE]) as provider:
        stream = make_client(provider, timeout=0.5).stream(MESSAGES)
        fragments = [await asyncio.wait_for(anext(stream), 5)]
        await asyncio.sleep(1.0)  # the deadline is now in the past; the provider has sent everything
        error: GenerationError | None = None
        try:
            async for fragment in stream:
                fragments.append(fragment)
        except GenerationError as raised:
            error = raised
        finally:
            await stream.aclose()

    assert error is not None, f"the stream was read to its end: {len(fragments)} fragments"
    assert error.code == "generation_timeout"
    assert len(fragments) < 51
    assert_fixed_message(error)


async def test_the_deadline_is_enforced_even_when_no_read_has_to_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    # A fully buffered fast stream: every read of the body completes at once, without ever suspending, so an
    # `asyncio.timeout` alone would never fire. The consumer stays away past the deadline after the first fragment.
    chunks = [delta_event(f"parte{number} ") for number in range(50)] + [finish_event(), DONE]

    async def buffered(self: httpx.Response) -> Any:
        for chunk in chunks:
            yield chunk  # no await between the chunks

    monkeypatch.setattr(httpx.Response, "aiter_bytes", buffered)
    with fake_generation_provider() as provider:
        stream = make_client(provider, timeout=0.3).stream(MESSAGES)
        fragments = [await asyncio.wait_for(anext(stream), 5)]
        await asyncio.sleep(0.6)  # the deadline is now in the past
        error: GenerationError | None = None
        try:
            async for fragment in stream:
                fragments.append(fragment)
        except GenerationError as raised:
            error = raised
        finally:
            await stream.aclose()

    assert error is not None, f"the stream was read to its end: {len(fragments)} fragments"
    assert error.code == "generation_timeout"
    assert len(fragments) < 51
    assert_nothing_chained(error, QUESTION)


async def test_a_buffered_stream_that_is_read_in_time_is_not_cut(monkeypatch: pytest.MonkeyPatch) -> None:
    chunks = [delta_event("uno"), delta_event(" due"), finish_event(), DONE]

    async def buffered(self: httpx.Response) -> Any:
        for chunk in chunks:
            yield chunk

    monkeypatch.setattr(httpx.Response, "aiter_bytes", buffered)
    with fake_generation_provider() as provider:
        assert await read_all(make_client(provider, timeout=10)) == (["uno", " due"], None)
