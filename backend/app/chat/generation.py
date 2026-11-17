"""Client of an OpenAI-compatible chat API in streaming mode (phase 3: REQ-001, REQ-002, REQ-008).

`OpenAICompatibleClient.stream` posts `{base_url}/chat/completions` with `stream: true` and yields the text fragments
(`choices[0].delta.content`) as the provider sends them; `check` asks `GET {base_url}/models` for the health check.

Every failure is a `GenerationError` with a fixed message: never the prompt, the body received or the key. It holds no
other exception, neither as `__cause__` nor as `__context__`: the repr of the original (an encoding error, a JSON error)
can carry the key or the prompt. Rules for a stream that is not normal (each one is a `generation_error`, the text
already yielded is the caller's to discard):
- a status that is not 2xx (a redirect is never followed: the prompt and the key go to the configured URL only), a
  connection that cannot be made or breaks, a body that is not an event stream (a 200 with a JSON error body
  included), an event that is not valid JSON of the expected shape, an `error` object in an event, a line longer than
  `_MAX_LINE_BYTES`, a key or a message that cannot be encoded (a non-ASCII key, a lone surrogate);
- a `finish_reason` other than `stop` (`length`, `content_filter`, `error`, anything else): `stop` is the only normal
  end, so a cut answer is never saved as a completed one (owner's decision, 2026-11-17);
- a stream that ends, normally or not, without a single non-empty text fragment (owner's decision, 2026-11-17);
- a stream that ends without `data: [DONE]` and without a `finish_reason` (truncated: the strictest safe reading).
  A `finish_reason` of `stop` followed by the end of the stream, with no `[DONE]`, is a normal end.
Events without text (empty `choices`, a `delta` with no `content`, no `choices` at all, comments, other SSE fields) are
skipped. The lines are split on `\\n` in the bytes, before decoding: a multibyte character split between packets is
whole by then, and the Unicode line separators (U+2028, U+0085...) that `aiter_lines` would split on can sit inside the
text. The time limit covers the whole generation: connection, headers and every read, up to `timeout` seconds from
the first iteration of `stream` (the body of an async generator starts then, not at the call). The deadline is also
compared with the clock before every read, so it holds when the data is already buffered and no read has to wait
(`generation_timeout`).
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Sequence
from contextlib import aclosing
from typing import Any, Literal, NamedTuple, TypedDict

import httpx
from pydantic import SecretStr

ErrorCode = Literal["generation_error", "generation_timeout"]

_MESSAGES: dict[ErrorCode, str] = {
    "generation_error": "The generation provider failed or sent an invalid answer",
    "generation_timeout": "The generation provider did not finish in time",
}
# The fields of an event stream; only `data` carries what is read, the others are skipped
_FIELDS = frozenset({"data", "event", "id", "retry"})
# A line of the stream (a whole event of text is far below this); longer is not an answer of a chat provider
_MAX_LINE_BYTES = 1024 * 1024


class ChatMessage(TypedDict):
    role: str
    content: str


class GenerationError(Exception):
    """The generation failed: `code` is what the caller saves and shows; the message is fixed text."""

    def __init__(self, code: ErrorCode, message: str | None = None) -> None:
        super().__init__(message or _MESSAGES[code])
        self.code: ErrorCode = code


class _Parsed(NamedTuple):
    text: str = ""
    finished: bool = False  # the event has the finish_reason `stop`
    done: bool = False  # the event is `[DONE]`


_SKIP = _Parsed()


def _invalid() -> GenerationError:
    return GenerationError("generation_error")


def _parse_line(raw: bytes) -> _Parsed:
    """One line of the stream: its text, whether it finishes the answer, whether it is `[DONE]`."""
    try:
        line = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise _invalid() from None
    line = line.removesuffix("\r")
    if not line or line.startswith(":"):
        return _SKIP
    field, _, value = line.partition(":")
    if field not in _FIELDS:
        raise _invalid()  # not an event stream (for example the JSON body of an error sent with 200)
    if field != "data":
        return _SKIP
    value = value.removeprefix(" ")
    if value.strip() == "[DONE]":
        return _Parsed(done=True)
    try:
        event: Any = json.loads(value)
    except (ValueError, RecursionError):
        raise _invalid() from None
    if not isinstance(event, dict) or event.get("error") is not None:
        raise _invalid()
    choices = event.get("choices")
    if choices is None or choices == []:
        return _SKIP  # usage or prompt-filter metadata
    if not isinstance(choices, list) or not isinstance(choices[0], dict):
        raise _invalid()
    choice: dict[str, Any] = choices[0]
    reason = choice.get("finish_reason")
    if reason is not None and reason != "stop":
        raise _invalid()  # length, content_filter, error, ...: the answer is cut or refused
    delta = choice.get("delta")
    content = None
    if delta is not None:
        if not isinstance(delta, dict):
            raise _invalid()
        content = delta.get("content")
    if content is not None and not isinstance(content, str):
        raise _invalid()
    return _Parsed(text=content or "", finished=reason == "stop")


async def _within[T](deadline: float, awaitable: Awaitable[T]) -> T:
    """Wait for `awaitable`, at most until `deadline` (a `loop.time()` value); TimeoutError after it."""
    async with asyncio.timeout_at(deadline):
        return await awaitable


class OpenAICompatibleClient:
    def __init__(self, base_url: str, model: str, api_key: str | None, timeout: float) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        # A SecretStr: the key does not show in the repr of the client or of its attributes
        self._api_key = SecretStr(api_key.strip()) if api_key and api_key.strip() else None
        self.timeout = timeout

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._api_key.get_secret_value()}"} if self._api_key else {}

    async def stream(self, messages: Sequence[ChatMessage]) -> AsyncIterator[str]:
        """The text fragments of the answer, in order; `GenerationError` when it fails or does not end in time."""
        deadline = asyncio.get_running_loop().time() + self.timeout
        failure: ErrorCode
        try:
            # No httpx timeout: the deadline above is the only one, so that it covers the whole generation. The
            # `yield` stays outside of every timeout block, so that the time the caller spends with a fragment does
            # not cancel the caller.
            async with httpx.AsyncClient(timeout=None) as client:
                request = client.build_request(
                    "POST",
                    f"{self.base_url}/chat/completions",
                    headers=self._headers(),
                    json={"model": self.model, "messages": list(messages), "stream": True},
                )
                response = await _within(deadline, client.send(request, stream=True))
                try:
                    if not response.is_success:
                        raise _invalid()
                    async with aclosing(self._fragments(response, deadline)) as fragments:
                        async for text in fragments:
                            yield text
                finally:
                    await response.aclose()
        except GenerationError as error:
            failure = error.code
        except TimeoutError:
            failure = "generation_timeout"
        except (httpx.HTTPError, httpx.InvalidURL, httpx.StreamError, OSError, UnicodeError):
            # UnicodeError: a key with a non-ASCII character (the header) or a lone surrogate in a message (the JSON)
            failure = "generation_error"
        else:
            return
        # Raised here, outside of the handlers above: no `__context__` links it to the original exception
        raise GenerationError(failure)

    async def _fragments(self, response: httpx.Response, deadline: float) -> AsyncGenerator[str]:
        chunks = response.aiter_bytes().__aiter__()
        loop = asyncio.get_running_loop()
        buffer = bytearray()
        finished = False
        has_text = False
        while True:
            if loop.time() >= deadline:  # `asyncio.timeout` fires only when a read suspends: a buffered one would not
                raise TimeoutError
            chunk = await _within(deadline, anext(chunks, None))
            if chunk is not None:
                buffer.extend(chunk)
            # at the end of the stream, what is left is a last line without its newline
            lines: list[bytes] = []
            while (end := buffer.find(b"\n")) >= 0:
                lines.append(bytes(buffer[:end]))
                del buffer[: end + 1]
            if chunk is None and buffer:
                lines.append(bytes(buffer))
                buffer.clear()
            if len(buffer) > _MAX_LINE_BYTES or any(len(line) > _MAX_LINE_BYTES for line in lines):
                raise _invalid()
            for line in lines:
                parsed = _parse_line(line)
                if parsed.text:
                    has_text = True
                    yield parsed.text
                finished = finished or parsed.finished
                if parsed.done:
                    if not has_text:
                        raise _invalid()  # an empty answer is not an answer
                    return
            if chunk is None:
                break
        if not finished or not has_text:
            raise _invalid()  # closed without [DONE] and without a finish_reason (the answer may be cut), or empty

    async def check(self, timeout: float) -> Literal["ok", "unreachable"]:  # noqa: ASYNC109 (the plan's interface)
        """`ok` if `GET {base_url}/models` answers 2xx within `timeout` seconds; `unreachable` otherwise."""
        try:
            async with asyncio.timeout(timeout), httpx.AsyncClient(timeout=timeout) as client:
                response = await client.get(f"{self.base_url}/models", headers=self._headers())
        except Exception:
            return "unreachable"
        return "ok" if response.is_success else "unreachable"
