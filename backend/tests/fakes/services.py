"""Fake Ollama and Docling servers: real sockets, no external network, standard library only.

In a test, `with fake_ollama() as ollama:` serves on a free local port (`ollama.url`), records every embedding request
(`ollama.requests`) and answers with deterministic vectors: `fake_vector(text, dimensions)`, derived from the hash
of the text, so a test can tell which vector belongs to which text. The failure modes are fields of the object and
may be changed while the server runs.

`python -m tests.fakes.services` serves both fakes for the end-to-end tests inside a container (Ollama on 11434,
Docling on 5001, the dimensions from EMBEDDING_DIMENSIONS); it needs nothing else from the project.
"""

from __future__ import annotations

import hashlib
import json
import os
import random
import signal
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

OLLAMA_PORT = 11434
DOCLING_PORT = 5001
DEFAULT_DIMENSIONS = 1024


def fake_vector(text: str, dimensions: int) -> list[float]:
    """The vector the fake Ollama returns for a text: `dimensions` numbers in [-1, 1) from the SHA-256 of the text."""
    seed = int.from_bytes(hashlib.sha256(text.encode("utf-8")).digest(), "big")
    generator = random.Random(seed)
    return [generator.uniform(-1.0, 1.0) for _ in range(dimensions)]


@dataclass(frozen=True)
class EmbedRequest:
    """One POST /api/embed as the fake received it."""

    model: object
    texts: list[str]


@dataclass
class FakeOllama:
    url: str = ""
    dimensions: int = DEFAULT_DIMENSIONS
    # Every request received, failed ones included, in arrival order.
    requests: list[EmbedRequest] = field(default_factory=list)
    # Failure modes. From the request number `fail_from_request` (1 is the first) on, `fail_status` is answered
    # instead of the vectors.
    fail_status: int | None = None
    fail_from_request: int = 1
    # Answer 200 with a body that is not {"embeddings": [[number, ...], ...]}.
    invalid_body: bool = False
    # The answer has this many vectors more (or fewer, if negative) than the request has texts.
    vector_count_delta: int = 0
    # The same for one request only: {request number (1 is the first): delta}, added to `vector_count_delta`.
    vector_count_deltas: dict[int, int] = field(default_factory=dict)
    # Seconds to wait before answering, for the timeout tests.
    delay_seconds: float = 0.0
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def texts_sent(self) -> list[str]:
        return [text for request in self.requests for text in request.texts]


def _json(handler: BaseHTTPRequestHandler, status: int, body: Any) -> None:
    payload = json.dumps(body).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(payload)))
    handler.end_headers()
    handler.wfile.write(payload)


class _Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], handler: type[BaseHTTPRequestHandler], state: Any) -> None:
        super().__init__(address, handler)
        self.state = state


class _Quiet(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        return


class _OllamaHandler(_Quiet):
    server: _Server

    def do_GET(self) -> None:  # noqa: N802 (http.server API)
        if self.path == "/api/version":
            _json(self, 200, {"version": "0.0.0-fake"})
        else:
            _json(self, 404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802 (http.server API)
        state: FakeOllama = self.server.state
        if self.path != "/api/embed":
            _json(self, 404, {"error": "not found"})
            return
        raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        try:
            body = json.loads(raw)
            texts = body["input"]
            if not isinstance(texts, list) or not all(isinstance(text, str) for text in texts):
                raise ValueError("input must be a list of strings")
        except (ValueError, KeyError, TypeError):
            _json(self, 400, {"error": "invalid request"})
            return
        with state.lock:
            state.requests.append(EmbedRequest(model=body.get("model"), texts=list(texts)))
            number = len(state.requests)
        if state.delay_seconds:
            time.sleep(state.delay_seconds)
        if state.fail_status is not None and number >= state.fail_from_request:
            _json(self, state.fail_status, {"error": "failure on purpose"})
        elif state.invalid_body:
            _json(self, 200, {"embeddings": "not a list of vectors"})
        else:
            count = max(0, len(texts) + state.vector_count_delta + state.vector_count_deltas.get(number, 0))
            vectors = [
                fake_vector(texts[index] if index < len(texts) else f"extra {index}", state.dimensions)
                for index in range(count)
            ]
            _json(self, 200, {"model": body.get("model"), "embeddings": vectors})


class _DoclingHandler(_Quiet):
    def do_GET(self) -> None:  # noqa: N802 (http.server API)
        if self.path == "/health":
            _json(self, 200, {"status": "ok"})
        else:
            _json(self, 404, {"detail": "not found"})

    def do_POST(self) -> None:  # noqa: N802 (http.server API)
        self.rfile.read(int(self.headers.get("Content-Length", "0")))
        # Converting files is the work of T-05; until then the fake only answers the health check.
        _json(self, 501, {"detail": "the Docling fake does not convert files yet"})


def _start(server: _Server) -> threading.Thread:
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return thread


@contextmanager
def fake_ollama(dimensions: int = DEFAULT_DIMENSIONS, **modes: Any) -> Iterator[FakeOllama]:
    """A fake Ollama on a free local port; `modes` set the failure fields of FakeOllama."""
    state = FakeOllama(dimensions=dimensions, **modes)
    server = _Server(("127.0.0.1", 0), _OllamaHandler, state)
    state.url = f"http://127.0.0.1:{server.server_address[1]}"
    _start(server)
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()


def main() -> None:
    dimensions = int(os.environ.get("EMBEDDING_DIMENSIONS") or DEFAULT_DIMENSIONS)
    servers = [
        _Server(("0.0.0.0", OLLAMA_PORT), _OllamaHandler, FakeOllama(dimensions=dimensions)),
        _Server(("0.0.0.0", DOCLING_PORT), _DoclingHandler, None),
    ]
    for server in servers:
        _start(server)
    stop = threading.Event()
    for name in ("SIGTERM", "SIGINT"):
        signal.signal(getattr(signal, name), lambda *_: stop.set())
    print(f"fake Ollama on {OLLAMA_PORT}, fake Docling on {DOCLING_PORT}", flush=True)
    stop.wait()
    for server in servers:
        server.shutdown()


if __name__ == "__main__":
    main()
